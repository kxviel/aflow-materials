"""Download and normalize one assigned AFLOW index part into AUID bucket folders."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import signal
import sqlite3
import sys
import time
from functools import partial
from importlib import import_module
from importlib.metadata import version
from pathlib import Path
from urllib.parse import quote

from aflow_transfer import (
    FetchError,
    Transfer,
    ValidationError,
    file_digest,
    index_rows,
    listed_files,
    material_url,
    scan_index,
    validate_transfer_args,
)
from download_index import IndexErrorBase, atomic_json, now, output_lock

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger(__name__)


class Normalizer:
    """Load the existing scientific rules once, preserving raw/derived provenance."""

    def __init__(self, config):
        try:
            for package in ("pymatgen.core", "spglib", "yaml"):
                import_module(package)
        except ImportError as exc:
            raise ValidationError(
                "Normalization dependencies are missing. Use the existing .venv/bin/python "
                "or install the environment with 'uv sync --locked' from the project root."
            ) from exc
        source = ROOT / "src"
        sys.path.insert(0, str(source))
        from aflow.aflow import merge_aflow_record, prepare_aflow_record
        from aflow.constants import CONSISTENCY_FIELDS
        from aflow.normalize_reported_values import (
            _build_sg_reference,
            load_config,
            normalize_row,
        )

        self.mapping = load_config(config, "aflow")
        if (
            not {"material_id", "formula", "geometry", "spacegroup"}
            <= self.mapping.keys()
        ):
            raise ValidationError(
                "AFLOW normalization mapping is missing required fields"
            )
        _build_sg_reference()
        self.prepare = prepare_aflow_record
        self.normalize_row = normalize_row
        self.merge = merge_aflow_record
        self.checks = CONSISTENCY_FIELDS
        digest = hashlib.sha256()
        for path in sorted((source / "aflow").glob("*.py")):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        digest.update(Path(config).read_bytes())
        self.versions = {
            pkg: version(pkg) for pkg in ("pymatgen", "spglib", "numpy", "PyYAML")
        }
        digest.update(json.dumps(self.versions, sort_keys=True).encode())
        self.fingerprint = digest.hexdigest()

    def __call__(self, raw, row_number):
        row, issues = self.normalize_row(
            self.prepare(raw),
            self.mapping,
            row_number,
            "aflow",
            {},
            enable_geometry=True,
        )
        intermediary = {
            "auid": raw["auid"],
            "raw": raw,
            "normalized": {
                key: value
                for key, value in row.items()
                if key.startswith("normalized_") and value != ""
            },
            "derived": {
                key: value
                for key, value in row.items()
                if key.startswith(("derived_", "qc_")) and value != ""
            },
            "consistency": {key: row[key] for key in self.checks if row.get(key) != ""},
            "quality_status": row["quality_status"],
            "issues": issues,
        }
        normalized = self.merge(intermediary, self.mapping)
        normalized["provenance"]["intermediary_file"] = "intermediary.json"
        normalized["provenance"]["raw_file"] = "raw.json"
        return intermediary, normalized


class Progress:
    def __init__(self, directory, args, assignment, normalizer):
        self.directory = directory
        self.db = sqlite3.connect(directory / "progress.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS materials(
                auid TEXT PRIMARY KEY,aurl TEXT NOT NULL,position INTEGER NOT NULL UNIQUE,
                complete INTEGER NOT NULL DEFAULT 0,quality_status TEXT
            );
            CREATE TABLE IF NOT EXISTS artifacts(
                auid TEXT NOT NULL,target TEXT NOT NULL,size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(auid,target)
            );
            CREATE TABLE IF NOT EXISTS failures(
                id INTEGER PRIMARY KEY,auid TEXT NOT NULL,stage TEXT NOT NULL,target TEXT NOT NULL,
                url TEXT,attempt INTEGER NOT NULL,timestamp TEXT NOT NULL,error TEXT NOT NULL,
                http_status INTEGER,curl_exit_code INTEGER,resolved_at TEXT
            );
        """)
        config = {
            "schema_version": 1,
            "input_sha256": assignment["sha256"],
            "assigned_records": assignment["records"],
            "bucket_prefix_chars": 3,
            "normalization_fingerprint": normalizer.fingerprint,
        }
        previous = self.get("config")
        if previous is not None and previous != config:
            self.db.close()
            raise ValidationError(
                "This output directory belongs to a different assignment or normalization "
                "version. Keep the original input/environment or choose another output directory."
            )
        if previous is None:
            with self.db:
                self.set("config", config)
                self.set("created_at", now())
        self.assignment = assignment
        self.args = args
        self.versions = normalizer.versions
        self.last_report = 0.0

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value))
        )

    def material(self, position, entry):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO materials(auid,aurl,position) VALUES (?,?,?)",
                (entry["auid"], entry["aurl"], position),
            )
        return self.db.execute(
            "SELECT * FROM materials WHERE auid=?", (entry["auid"],)
        ).fetchone()

    def matches(self, auid, target, path):
        row = self.db.execute(
            "SELECT * FROM artifacts WHERE auid=? AND target=?", (auid, target)
        ).fetchone()
        if row is None or not path.is_file():
            return False
        stat = path.stat()
        return stat.st_size == row["size"] and (
            stat.st_mtime_ns == row["mtime_ns"] or file_digest(path) == row["sha256"]
        )

    def artifact(self, auid, target, path, stage):
        stat = path.stat()
        digest = file_digest(path)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?)",
                (auid, target, stat.st_size, stat.st_mtime_ns, digest),
            )
            self.resolve(auid, stage, target)

    def resolve(self, auid, stage, target):
        self.db.execute(
            """
            UPDATE failures SET resolved_at=?
            WHERE auid=? AND stage=? AND target=? AND resolved_at IS NULL
        """,
            (now(), auid, stage, target),
        )

    def failure(self, auid, stage, target, url, attempt, error):
        event = {
            "auid": auid,
            "stage": stage,
            "target": target,
            "url": url,
            "attempt": attempt,
            "timestamp": now(),
            "error": str(error),
            "http_status": getattr(error, "status", None),
            "curl_exit_code": getattr(error, "curl_code", None),
        }
        with self.db:
            self.db.execute(
                """
                INSERT INTO failures(auid,stage,target,url,attempt,timestamp,error,http_status,curl_exit_code)
                VALUES (:auid,:stage,:target,:url,:attempt,:timestamp,:error,:http_status,:curl_exit_code)
            """,
                event,
            )
        # A streaming event log stays cheap even when many files fail. SQLite is authoritative.
        with (self.directory / "failures.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def finish_material(self, auid, complete, quality):
        with self.db:
            self.db.execute(
                "UPDATE materials SET complete=?,quality_status=? WHERE auid=?",
                (int(complete), quality, auid),
            )

    def reports(self, status, error=None, final=False):
        completed = self.db.execute(
            "SELECT COUNT(*) FROM materials WHERE complete=1"
        ).fetchone()[0]
        unresolved = self.db.execute(
            "SELECT COUNT(*) FROM failures WHERE resolved_at IS NULL"
        ).fetchone()[0]
        atomic_json(
            self.directory / "manifest.json",
            {
                **self.get("config"),
                "input": str(self.args.input),
                "created_at": self.get("created_at"),
                "updated_at": now(),
                "status": status,
                "completed_materials": completed,
                "remaining_materials": self.assignment["records"] - completed,
                "unresolved_failure_attempts": unresolved,
                "last_error": error,
                "normalization_versions": self.versions,
                "bandwidth_mbps": self.args.bandwidth_mbps,
            },
        )
        self.last_report = time.monotonic()
        if final:
            temporary = self.directory / "failures.json.tmp"
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write('{"failures":[\n')
                separator = ""
                for row in self.db.execute("SELECT * FROM failures ORDER BY id"):
                    item = dict(row)
                    item["resolved"] = item["resolved_at"] is not None
                    stream.write(separator + json.dumps(item, ensure_ascii=False))
                    separator = ",\n"
                stream.write("\n]}\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / "failures.json")
        return completed


def validate_record(path, auid):
    try:
        raw = json.loads(path.read_bytes())
    except (ValueError, UnicodeError) as exc:
        raise ValidationError(f"Invalid material JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("auid") != auid:
        raise ValidationError("Full record AUID does not match the assigned AUID")
    listed_files(raw)
    if "aurl" in raw:
        material_url(raw["aurl"])
    return raw


def process_material(position, entry, folder, state, transfer, normalize):
    auid = entry["auid"]
    folder.mkdir(parents=True, exist_ok=True)
    raw_path = folder / "raw.json"
    metadata_refetched = False
    if state.matches(auid, "raw.json", raw_path):
        raw = validate_record(raw_path, auid)
    else:
        metadata_refetched = True
        raw = None
        base = material_url(entry["aurl"])
        for endpoint in ("?format=json", "aflowlib.json"):
            url = base + endpoint
            try:
                raw = transfer.download(
                    url,
                    raw_path,
                    partial(state.failure, auid, "metadata", "raw.json", url),
                    validate=lambda path: validate_record(path, auid),
                    max_bytes=32 * 1024 * 1024,
                )
                state.artifact(auid, "raw.json", raw_path, "metadata")
                break
            except FetchError as exc:
                if exc.retryable:
                    raise
            except ValidationError:
                pass  # Try AFLOW's second supported full-record endpoint.
        if raw is None:
            state.finish_material(auid, False, None)
            return False

    normalized_ok, quality = False, None
    intermediary_path, normalized_path = (
        folder / "intermediary.json",
        folder / "normalized.json",
    )
    if (
        not metadata_refetched
        and state.matches(auid, "intermediary.json", intermediary_path)
        and state.matches(auid, "normalized.json", normalized_path)
    ):
        quality = json.loads(normalized_path.read_bytes())["quality"]["status"]
        normalized_ok = True
    else:
        try:
            intermediary, normalized = normalize(raw, position)
            atomic_json(intermediary_path, intermediary)
            atomic_json(normalized_path, normalized)
            state.artifact(
                auid, "intermediary.json", intermediary_path, "normalization"
            )
            state.artifact(auid, "normalized.json", normalized_path, "normalization")
            quality = normalized["quality"]["status"]
            normalized_ok = True
        except Exception as exc:
            LOG.exception("Normalization failed for %s", auid)
            state.failure(auid, "normalization", "normalized.json", None, 1, exc)

    base = material_url(raw.get("aurl", entry["aurl"]))
    files_ok = True
    for filename in listed_files(raw):
        target = f"files/{filename}"
        destination = folder / target
        if state.matches(auid, target, destination):
            continue
        url = base + quote(filename, safe="")
        try:
            transfer.download(
                url,
                destination,
                partial(state.failure, auid, "file", target, url),
            )
            state.artifact(auid, target, destination, "file")
        except FetchError as exc:
            files_ok = False
            if exc.retryable:
                raise  # Stop on an outage; preserve all completed artifacts for resume.
            print(f"  File unavailable for {auid}: {filename}: {exc}", flush=True)
    complete = normalized_ok and files_ok
    state.finish_material(auid, complete, quality)
    return complete


def run(args):
    print("Validating assignment…", flush=True)
    assignment = scan_index(args.input)
    assignments_path = args.input.parent / "assignments.json"
    if assignments_path.exists():
        manifest = json.loads(assignments_path.read_text())
        matches = [
            item
            for item in manifest.get("assignments", [])
            if item["file"] == args.input.name
        ]
        if len(matches) != 1 or any(
            matches[0].get(key) != value for key, value in assignment.items()
        ):
            raise ValidationError(
                "Assignment does not match its split manifest/checksum"
            )
    normalizer = Normalizer(args.config)
    transfer = Transfer(
        args.bandwidth_mbps, args.delay, args.timeout, args.attempts, args.retry_delay
    )
    with output_lock(args.output_dir):
        state = Progress(args.output_dir, args, assignment, normalizer)
        try:
            state.reports("in_progress")
            processed = 0
            for position, entry, _ in index_rows(args.input):
                previous = state.material(position, entry)
                if previous["complete"] and not args.verify_completed:
                    continue
                if args.max_records and processed >= args.max_records:
                    state.reports("paused", final=True)
                    print(
                        "Paused at --max-records. Rerun without it to continue.",
                        flush=True,
                    )
                    return 0
                suffix = entry["auid"].split(":", 1)[1]
                folder = args.output_dir / suffix[:3] / suffix
                state.finish_material(entry["auid"], False, previous["quality_status"])
                print(
                    f"[{position:,}/{assignment['records']:,}] {entry['auid']} → {folder}",
                    flush=True,
                )
                process_material(position, entry, folder, state, transfer, normalizer)
                processed += 1
                if time.monotonic() - state.last_report >= 30:
                    state.reports("in_progress")
            complete = state.db.execute(
                "SELECT COUNT(*) FROM materials WHERE complete=1"
            ).fetchone()[0]
            finished = complete == assignment["records"]
            state.reports("complete" if finished else "incomplete", final=True)
            print(
                f"Finished pass: {complete:,}/{assignment['records']:,} materials complete. "
                "See manifest.json and failures.json.",
                flush=True,
            )
            return 0 if finished else 1
        except KeyboardInterrupt:
            state.reports(
                "interrupted", "Interrupted; completed artifacts are saved", final=True
            )
            print("\nInterrupted. Rerun the same command to resume.", file=sys.stderr)
            return 130
        except (IndexErrorBase, OSError, sqlite3.Error, ValueError) as exc:
            state.reports("failed", str(exc), final=True)
            print(
                f"Stopped: {exc}. Completed artifacts are saved; rerun to resume.",
                file=sys.stderr,
            )
            return 1
        finally:
            state.db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", required=True, type=Path, help="assigned part_N.jsonl"
    )
    parser.add_argument(
        "--output-dir", type=Path, help="default: data/downloads/<input filename stem>"
    )
    parser.add_argument("--config", type=Path, default=ROOT / "lib/field_map.yaml")
    parser.add_argument(
        "--bandwidth-mbps",
        type=float,
        default=0,
        help="average transfer limit; 0 unlimited for HPC (default); use 5 on Wi-Fi",
    )
    parser.add_argument("--delay", type=float, default=0.3)
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="metadata deadline and file low-speed timeout in seconds (default: 300)",
    )
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--retry-delay", type=float, default=5)
    parser.add_argument(
        "--max-records",
        type=int,
        default=0,
        help="pause after this many unfinished materials; 0 all",
    )
    parser.add_argument(
        "--verify-completed",
        action="store_true",
        help="also recheck artifact sizes/mtimes for completed materials",
    )
    args = parser.parse_args(argv)
    validate_transfer_args(parser, args)
    if shutil.which("curl") is None:
        parser.error("curl is required")
    args.input = args.input.expanduser().resolve()
    args.output_dir = (
        (args.output_dir or ROOT / "data/downloads" / args.input.stem)
        .expanduser()
        .resolve()
    )
    args.config = args.config.expanduser().resolve()
    try:
        return run(args)
    except (IndexErrorBase, OSError, sqlite3.Error, ValueError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main())
