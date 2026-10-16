"""Download the complete AFLOW AUID/AURL index, with resumable page checkpoints.

Requires Python 3.10+ and curl on Linux/macOS. No Python packages are needed.
Run `python3 download_index.py --help` for bandwidth and resume options.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

ENDPOINT = "https://aflow.org/API/aflux/"
QUERY = "auid,aurl,$compound,$spacegroup_relax,$Pearson_symbol_relax"
AUID = re.compile(r"aflow:[0-9a-f]{16}\Z")
ROW_LABEL = re.compile(r"([1-9][0-9]*) of ([1-9][0-9]*)\Z")
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class IndexErrorBase(Exception):
    """An actionable download or validation problem."""


class ValidationError(IndexErrorBase):
    pass


class FetchError(IndexErrorBase):
    def __init__(
        self, message, *, status=None, curl_code=None, retryable=True, retry_after=0.0
    ):
        super().__init__(message)
        self.status = status
        self.curl_code = curl_code
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass
class Page:
    number: int
    total: int
    rows: list[tuple[int, str, str]]
    sha256: str
    size: int
    seconds: float


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValidationError(f"Duplicate JSON key in API response: {key!r}")
        obj[key] = value
    return obj


def parse_page(body: bytes, number: int, page_size: int, seconds=0.0) -> Page:
    try:
        data = json.loads(body, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as exc:
        raise FetchError(f"Invalid JSON response: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise ValidationError(
            "Expected a nonempty AFLUX page with numbered result keys"
        )

    rows, totals = [], set()
    for label, record in data.items():
        match = ROW_LABEL.fullmatch(label)
        if not match or not isinstance(record, dict):
            raise ValidationError(f"Unexpected AFLUX response item: {label!r}")
        position, total = map(int, match.groups())
        totals.add(total)
        auid, aurl = record.get("auid"), record.get("aurl")
        if not isinstance(auid, str) or not AUID.fullmatch(auid):
            raise ValidationError(f"Invalid AUID at row {position}: {auid!r}")
        if (
            not isinstance(aurl, str)
            or not aurl
            or ":" not in aurl
            or any(char.isspace() for char in aurl)
        ):
            raise ValidationError(f"Missing/invalid AURL for {auid}: {aurl!r}")
        rows.append((position, auid, aurl))
    if len(totals) != 1:
        raise ValidationError("API reported different totals within one page")
    total = totals.pop()
    start = (number - 1) * page_size + 1
    end = min(number * page_size, total)
    rows.sort()
    if start > total or [row[0] for row in rows] != list(range(start, end + 1)):
        raise ValidationError(
            f"Page {number} must contain exactly rows {start}-{end}; "
            f"received {len(rows)} rows with range {rows[0][0]}-{rows[-1][0]}"
        )
    for left, right in pairwise(rows):
        if left[1] == right[1] and left[2] != right[2]:
            raise ValidationError(f"Conflicting AURLs for duplicate AUID {left[1]}")
        if left[1] > right[1]:
            raise ValidationError("Page contains out-of-order AUIDs")
    return Page(
        number, total, rows, hashlib.sha256(body).hexdigest(), len(body), seconds
    )


def atomic_json(path: Path, data) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def output_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".download.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValidationError(f"Another downloader is using {directory}") from exc
        lock.seek(0)
        lock.truncate()
        lock.write(f"{os.getpid()}\n")
        lock.flush()
        yield
        # Closing releases the lock, including on an exception. Keep its inode.


class State:
    def __init__(self, directory: Path, args):
        self.directory = directory
        self.args = args
        self.db = sqlite3.connect(directory / "index.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records (
                auid TEXT PRIMARY KEY, aurl TEXT NOT NULL, position INTEGER NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS pages (
                page INTEGER PRIMARY KEY, row_count INTEGER NOT NULL,
                first_auid TEXT NOT NULL, last_auid TEXT NOT NULL,
                response_sha256 TEXT NOT NULL, response_bytes INTEGER NOT NULL,
                fetch_seconds REAL NOT NULL, completed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS failures (
                id INTEGER PRIMARY KEY, page INTEGER NOT NULL, url TEXT NOT NULL,
                attempt INTEGER NOT NULL, timestamp TEXT NOT NULL, error TEXT NOT NULL,
                http_status INTEGER, curl_exit_code INTEGER, resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS duplicates (
                position INTEGER PRIMARY KEY, auid TEXT NOT NULL, aurl TEXT NOT NULL,
                first_position INTEGER NOT NULL
            );
        """)
        config = {
            "schema_version": 2,
            "endpoint": args.endpoint,
            "query": QUERY,
            "page_size": args.page_size,
        }
        previous = self.get("config")
        if previous is not None and (
            previous.get("schema_version") not in (1, 2)
            or any(
                previous.get(key) != value
                for key, value in config.items()
                if key != "schema_version"
            )
        ):
            self.db.close()
            raise ValidationError(
                "Endpoint, query, and page size must match the existing checkpoint. "
                "Use the original settings or a new --output-dir."
            )
        if previous is None:
            with self.db:
                self.set("config", config)
                self.set("created_at", now())
                self.set("status", "in_progress")
                self.set("unique_count", 0)
                self.set("duplicate_count", 0)
        elif previous.get("schema_version") == 1:
            # Upgrade old checkpoints in place, preserving every completed page.
            # Keep an SQLite-consistent backup of the pre-upgrade data.
            backup_path = directory / "index.sqlite3.v1.bak"
            if not backup_path.exists():
                backup = sqlite3.connect(backup_path)
                try:
                    self.db.backup(backup)
                finally:
                    backup.close()
            with self.db:
                self.set("config", config)
                self.set(
                    "unique_count",
                    self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                )
                self.set(
                    "duplicate_count",
                    self.db.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0],
                )

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row is not None else default

    def set(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value))
        )

    @property
    def completed_pages(self):
        return self.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]

    @property
    def records(self):
        return self.get("unique_count", 0)

    @property
    def source_rows(self):
        return self.db.execute(
            "SELECT COALESCE(SUM(row_count),0) FROM pages"
        ).fetchone()[0]

    @property
    def duplicate_rows(self):
        return self.get("duplicate_count", 0)

    def check_expected(self, total):
        if self.args.expected_count is not None and total != self.args.expected_count:
            raise ValidationError(
                f"API reports {total:,} source rows, but --expected-count is "
                f"{self.args.expected_count:,}. Confirm query coverage before proceeding."
            )

    def accept(self, page: Page):
        self.check_expected(page.total)
        known_total = self.get("api_total")
        if known_total is not None and known_total != page.total:
            raise ValidationError(
                f"API total changed from {known_total:,} to {page.total:,}. "
                "Preserving checkpoints; use a fresh output directory for a new collection."
            )
        if page.number != self.completed_pages + 1:
            raise ValidationError("Refusing a page that is not the next checkpoint")
        last = self.db.execute(
            "SELECT auid FROM records ORDER BY position DESC LIMIT 1"
        ).fetchone()
        if last and page.rows[0][1] < last[0]:
            raise ValidationError(
                "Out-of-order AUID across page boundaries; "
                "the source may have changed during pagination."
            )
        added, repeated = 0, 0
        try:
            with self.db:
                for position, auid, aurl in page.rows:
                    inserted = self.db.execute(
                        "INSERT INTO records(position,auid,aurl) VALUES (?,?,?) "
                        "ON CONFLICT(auid) DO NOTHING",
                        (position, auid, aurl),
                    ).rowcount
                    if inserted:
                        added += 1
                        continue
                    existing = self.db.execute(
                        "SELECT aurl,position FROM records WHERE auid=?", (auid,)
                    ).fetchone()
                    if existing["aurl"] != aurl:
                        raise ValidationError(
                            f"Conflicting AURLs for duplicate AUID {auid}"
                        )
                    self.db.execute(
                        "INSERT INTO duplicates VALUES (?,?,?,?)",
                        (position, auid, aurl, existing["position"]),
                    )
                    repeated += 1
                self.db.execute(
                    "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?)",
                    (
                        page.number,
                        len(page.rows),
                        page.rows[0][1],
                        page.rows[-1][1],
                        page.sha256,
                        page.size,
                        page.seconds,
                        now(),
                    ),
                )
                self.set("api_total", page.total)
                self.set("unique_count", self.records + added)
                self.set("duplicate_count", self.duplicate_rows + repeated)
                self.set("status", "in_progress")
                self.set("last_error", None)
                self.db.execute(
                    "UPDATE failures SET resolved_at=? WHERE page=? AND resolved_at IS NULL",
                    (now(), page.number),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"Duplicate AUID or row position: {exc}") from exc

    def log_failure(self, page, url, attempt, error):
        with self.db:
            self.db.execute(
                """
                INSERT INTO failures(page,url,attempt,timestamp,error,http_status,curl_exit_code)
                VALUES (?,?,?,?,?,?,?)
            """,
                (
                    page,
                    url,
                    attempt,
                    now(),
                    str(error),
                    getattr(error, "status", None),
                    getattr(error, "curl_code", None),
                ),
            )
            self.set("last_error", str(error))
        self.write_reports()

    def change_status(self, status, error=None):
        with self.db:
            self.set("status", status)
            self.set("last_error", error)
        self.write_reports()

    def write_reports(self):
        failures = []
        for row in self.db.execute("SELECT * FROM failures ORDER BY id"):
            item = dict(row)
            item["resolved"] = item["resolved_at"] is not None
            failures.append(item)
        atomic_json(self.directory / "failures.json", {"failures": failures})
        atomic_json(
            self.directory / "duplicates.json",
            {
                "duplicate_rows": self.duplicate_rows,
                "samples": [
                    dict(row)
                    for row in self.db.execute(
                        "SELECT * FROM duplicates ORDER BY position LIMIT 100"
                    )
                ],
                "sample_limit": 100,
                "full_log": {"database": "index.sqlite3", "table": "duplicates"},
            },
        )
        atomic_json(
            self.directory / "manifest.json",
            {
                **self.get("config"),
                "created_at": self.get("created_at"),
                "updated_at": now(),
                "status": self.get("status"),
                "api_total": self.get("api_total"),
                "records": self.records,
                "source_rows": self.source_rows,
                "duplicate_rows": self.duplicate_rows,
                "completed_pages": self.completed_pages,
                "expected_count": self.args.expected_count,
                "bandwidth_mbps": self.args.bandwidth_mbps,
                "delay_seconds": self.args.delay,
                "last_error": self.get("last_error"),
                "export": self.get("export"),
                "unresolved_failure_count": sum(not f["resolved"] for f in failures),
                "coverage_note": "Complete means the tested query passed count, ordinal and "
                "uniqueness checks. AFLUX pagination does not provide an immutable snapshot.",
            },
        )

    def export(self):
        total = self.get("api_total")
        count = self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        duplicates = self.db.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]
        pages = self.db.execute("SELECT COUNT(*), MAX(page) FROM pages").fetchone()
        if (
            total is None
            or self.source_rows != total
            or count != self.records
            or duplicates != self.duplicate_rows
            or count + duplicates != total
            or pages[0] != math.ceil(total / self.args.page_size)
            or pages[1] != pages[0]
        ):
            raise ValidationError(
                "Index is incomplete; refusing to export a master index"
            )
        self.check_expected(total)
        output = self.directory / "master_index.jsonl"
        saved_export = self.get("export")
        if saved_export and output.exists():
            digest = hashlib.sha256()
            with output.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() == saved_export["sha256"]:
                self.change_status("complete")
                return output

        temporary = output.with_suffix(".jsonl.part")
        digest = hashlib.sha256()
        exported = 0
        with temporary.open("wb") as stream:
            for row in self.db.execute("SELECT auid,aurl FROM records ORDER BY auid"):
                line = (
                    json.dumps(dict(row), separators=(",", ":"), ensure_ascii=False)
                    + "\n"
                ).encode("utf-8")
                stream.write(line)
                digest.update(line)
                exported += 1
            stream.flush()
            os.fsync(stream.fileno())
        if exported != count:
            raise ValidationError("Export count differs from the validated index count")
        os.replace(temporary, output)
        with self.db:
            self.set(
                "export",
                {
                    "file": output.name,
                    "records": exported,
                    "sha256": digest.hexdigest(),
                    "exported_at": now(),
                },
            )
            self.set("status", "complete")
            self.set("last_error", None)
        self.write_reports()
        return output


def retry_after_seconds(headers: Path) -> float:
    value = None
    for line in headers.read_text(encoding="iso-8859-1").splitlines():
        if line.startswith("HTTP/"):
            value = None
        elif line.lower().startswith("retry-after:"):
            value = line.split(":", 1)[1].strip()
    if value is None:
        return 0.0
    try:
        return max(0.0, float(int(value)))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0.0


class Client:
    def __init__(self, args):
        self.args = args
        self.last_finished = None

    def url(self, page):
        return f"{self.args.endpoint}?{QUERY},paging({page},{self.args.page_size})"

    def fetch(self, number):
        if self.last_finished is not None:
            time.sleep(
                max(0.0, self.args.delay - (time.monotonic() - self.last_finished))
            )
        body = self.args.output_dir / ".page.part"
        headers = self.args.output_dir / ".headers.part"
        command = [
            "curl",
            "--disable",
            "--silent",
            "--show-error",
            "--location",
            "--fail",
            "--proto",
            "=http,https",
            "--proto-redir",
            "=http,https",
            "--max-redirs",
            "5",
            "--connect-timeout",
            str(min(15.0, self.args.timeout)),
            "--max-time",
            str(self.args.timeout),
            "--max-filesize",
            str(MAX_RESPONSE_BYTES),
            "--user-agent",
            "aflow-master-index/1.0",
            "--output",
            str(body),
            "--dump-header",
            str(headers),
            "--write-out",
            "%{http_code}",
        ]
        if self.args.bandwidth_mbps > 0:
            command.extend(
                ["--limit-rate", str(max(1, round(self.args.bandwidth_mbps * 125000)))]
            )
        command.extend(["--url", self.url(number)])
        started = time.monotonic()
        process = None
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            try:
                stdout, stderr = process.communicate(timeout=self.args.timeout + 10)
            except subprocess.TimeoutExpired as exc:
                raise FetchError("curl exceeded the request deadline") from exc
            status = int(stdout.strip()) if stdout.strip().isdigit() else 0
            if process.returncode or status != 200:
                # A transfer can fail after the server has already sent HTTP 200
                # (for example a timeout or truncated body on an unstable Wi-Fi link).
                retryable = status in (
                    408,
                    425,
                    429,
                    500,
                    502,
                    503,
                    504,
                ) or process.returncode in (5, 6, 7, 16, 18, 28, 35, 52, 55, 56, 92)
                raise FetchError(
                    f"HTTP {status or 'unavailable'}, curl exit {process.returncode}: "
                    f"{stderr.strip()[:500]}",
                    status=status or None,
                    curl_code=process.returncode,
                    retryable=retryable,
                    retry_after=retry_after_seconds(headers)
                    if headers.exists()
                    else 0.0,
                )
            if body.stat().st_size > MAX_RESPONSE_BYTES:
                raise ValidationError("Response exceeds the page size safety limit")
            return parse_page(
                body.read_bytes(),
                number,
                self.args.page_size,
                time.monotonic() - started,
            )
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
            self.last_finished = time.monotonic()
            body.unlink(missing_ok=True)
            headers.unlink(missing_ok=True)


def download_page(client, state, page):
    for attempt in range(1, state.args.attempts + 1):
        try:
            result = client.fetch(page)
            state.accept(result)
            return result
        except (FetchError, ValidationError) as exc:
            state.log_failure(page, client.url(page), attempt, exc)
            if (
                not isinstance(exc, FetchError)
                or not exc.retryable
                or attempt == state.args.attempts
            ):
                raise
            delay = max(
                exc.retry_after,
                min(60.0, state.args.retry_delay * 2 ** min(attempt - 1, 30)),
            )
            print(
                f"Page {page}, attempt {attempt} failed: {exc}; retrying in {delay:g}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)


def run(args):
    with output_lock(args.output_dir):
        state = State(args.output_dir, args)
        try:
            total = state.get("api_total")
            if total is not None:
                state.check_expected(total)
            if state.get("status") == "complete":
                output = state.export()
                print(f"Already complete: {state.records:,} entries in {output}")
                return 0
            state.change_status("in_progress")
            client = Client(args)
            completed_this_run = 0
            started = time.monotonic()
            cap = (
                f"{args.bandwidth_mbps:g} Mbps" if args.bandwidth_mbps else "unlimited"
            )
            print(
                f"Master index only: page size {args.page_size:,}; transfer limit {cap}; "
                f"page delay {args.delay:g}s. Output: {args.output_dir}",
                flush=True,
            )
            if state.completed_pages:
                print(
                    f"Resuming after {state.completed_pages:,} pages / {state.source_rows:,} "
                    f"source rows ({state.records:,} unique entries).",
                    flush=True,
                )
            while total is None or state.completed_pages < math.ceil(
                total / args.page_size
            ):
                if args.max_pages and completed_this_run >= args.max_pages:
                    state.change_status("paused")
                    print(
                        "Paused at --max-pages; rerun without that option to continue.",
                        flush=True,
                    )
                    return 0
                result = download_page(client, state, state.completed_pages + 1)
                total = result.total
                completed_this_run += 1
                if result.number == 1:
                    print(
                        f"API reports {total:,} source rows; collecting all and deduplicating.",
                        flush=True,
                    )
                state.write_reports()
                pages = math.ceil(total / args.page_size)
                remaining = pages - state.completed_pages
                eta = (
                    remaining * (time.monotonic() - started) / completed_this_run / 3600
                )
                print(
                    f"Page {result.number:,}/{pages:,}: {state.source_rows:,}/{total:,} source rows "
                    f"({state.source_rows / total:.1%}); {state.records:,} unique, "
                    f"{state.duplicate_rows:,} duplicate rows; request {result.seconds:.1f}s; "
                    f"rough ETA {eta:.1f}h",
                    flush=True,
                )
            print("All pages validated. Exporting the sorted master index…", flush=True)
            output = state.export()
            print(f"Complete: {state.records:,} unique entries → {output}", flush=True)
            return 0
        except KeyboardInterrupt:
            state.change_status("interrupted", "Interrupted; committed pages are saved")
            print("\nInterrupted. Rerun the same command to resume.", file=sys.stderr)
            return 130
        except (IndexErrorBase, OSError, sqlite3.Error) as exc:
            state.change_status("failed", str(exc))
            print(
                f"Stopped: {exc}\nCommitted pages are saved. See failures.json and manifest.json.",
                file=sys.stderr,
            )
            return 1
        finally:
            state.db.close()


def arguments(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/master_index"),
        help="checkpoint and export directory (default: data/master_index)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=5000,
        help="entries per request; must stay unchanged when resuming (default: 5000)",
    )
    parser.add_argument(
        "--bandwidth-mbps",
        type=float,
        default=5.0,
        help="curl average transfer limit in decimal megabits/sec; 0 unlimited (default: 5)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="pause between page requests (default: 0.3s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="timeout per HTTP request (default: 120s)",
    )
    parser.add_argument(
        "--attempts", type=int, default=5, help="attempts per page (default: 5)"
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=2.0,
        help="initial exponential retry delay (default: 2s)",
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        help="optional strict API source-row total; stop if the API reports another count",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="pause after this many new pages in this invocation; 0 collects all",
    )
    parser.add_argument("--endpoint", default=ENDPOINT, help="AFLUX API base URL")
    args = parser.parse_args(argv)
    for name in ("bandwidth_mbps", "delay", "timeout", "retry_delay"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or name == "timeout" and value == 0:
            parser.error(
                f"--{name.replace('_', '-')} must be finite and {'positive' if name == 'timeout' else 'nonnegative'}"
            )
    if args.page_size <= 0 or args.attempts <= 0 or args.max_pages < 0:
        parser.error(
            "page size and attempts must be positive; max-pages must be nonnegative"
        )
    if args.expected_count is not None and args.expected_count <= 0:
        parser.error("--expected-count must be positive")
    parsed = urlsplit(args.endpoint)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        parser.error(
            "--endpoint must be an HTTP(S) base URL without query, fragment or credentials"
        )
    if shutil.which("curl") is None:
        parser.error("curl is required but was not found on PATH")
    args.output_dir = args.output_dir.expanduser().resolve()
    return args


def main(argv=None):
    args = arguments(argv)
    try:
        return run(args)
    except (IndexErrorBase, OSError, sqlite3.Error) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main())
