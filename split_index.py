"""Split a completed, sorted master index into disjoint per-account JSONL files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from aflow_transfer import (
    ValidationError,
    index_rows,
    scan_index,
)
from download_index import IndexErrorBase, atomic_json, now


def split(source, output, parts=5):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source.name.endswith((".part", ".tmp")):
        raise ValidationError("Use a completed master index, not a temporary export")
    if output.exists():
        raise ValidationError(
            f"Assignment output already exists: {output}; preserve existing assignments"
        )
    print("Validating and counting the completed master index…", flush=True)
    info = scan_index(source)
    if not info["records"]:
        raise ValidationError("Cannot split an empty master index")
    manifest_path = source.parent / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        export = manifest.get("export") or {}
        if (
            manifest.get("status") != "complete"
            or export.get("file") != source.name
            or export.get("records") != info["records"]
            or export.get("sha256") != info["sha256"]
        ):
            raise ValidationError(
                "The master completion manifest does not match this index"
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        base, remainder = divmod(info["records"], parts)
        rows = iter(index_rows(source))
        source_hash = hashlib.sha256()
        assignments = []
        start = 1
        for account in range(1, parts + 1):
            count = base + (account <= remainder)
            filename = f"part_{account}.jsonl"
            digest = hashlib.sha256()
            first = last = None
            with (staging / filename).open("wb") as stream:
                for _ in range(count):
                    _position, record, line = next(rows)
                    source_hash.update(line)
                    line = (json.dumps(record, separators=(",", ":")) + "\n").encode()
                    stream.write(line)
                    digest.update(line)
                    first = first or record["auid"]
                    last = record["auid"]
                stream.flush()
                os.fsync(stream.fileno())
            assignments.append(
                {
                    "account": account,
                    "file": filename,
                    "records": count,
                    "start_row": start if count else None,
                    "end_row": start + count - 1 if count else None,
                    "first_auid": first,
                    "last_auid": last,
                    "sha256": digest.hexdigest(),
                }
            )
            start += count
        if next(rows, None) is not None or source_hash.hexdigest() != info["sha256"]:
            raise ValidationError("Master index changed while it was being split")
        atomic_json(
            staging / "assignments.json",
            {
                "created_at": now(),
                "source": str(source),
                "source_sha256": info["sha256"],
                "records": info["records"],
                "parts": parts,
                "bucket_prefix_chars": 3,
                "assignments": assignments,
            },
        )
        if output.exists():
            raise ValidationError(
                "Assignment output appeared during splitting; refusing to replace it"
            )
        os.rename(staging, output)
        for part in assignments:
            print(
                f"{part['file']}: {part['records']:,} entries; "
                f"{part['first_auid']} … {part['last_auid']}"
            )
        return output
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", required=True, type=Path, help="completed master_index.jsonl"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data/assignments",
    )
    parser.add_argument("--parts", type=int, default=5)
    args = parser.parse_args(argv)
    if args.parts <= 0:
        parser.error("--parts must be positive")
    try:
        split(args.input, args.output_dir, args.parts)
        return 0
    except (IndexErrorBase, OSError, ValueError, StopIteration) as exc:
        print(f"Split stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
