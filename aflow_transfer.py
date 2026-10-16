"""Streaming index validation and rate-limited transfers for assignment workers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from download_index import (
    AUID,
    FetchError,
    ValidationError,
    retry_after_seconds,
)


def index_rows(path):
    """Read the sorted, unique, two-field JSONL produced by download_index.py."""
    previous = None
    with Path(path).open("rb") as stream:
        for number, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise ValidationError(
                    f"Invalid index JSON at line {number}: {exc}"
                ) from exc
            if not isinstance(record, dict) or set(record) != {"auid", "aurl"}:
                raise ValidationError(
                    f"Index line {number} must contain only auid and aurl"
                )
            auid = record["auid"]
            if not isinstance(auid, str) or not AUID.fullmatch(auid):
                raise ValidationError(f"Invalid AUID on index line {number}")
            material_url(record["aurl"])
            if previous is not None and auid <= previous:
                raise ValidationError(
                    f"Duplicate or unsorted AUID on index line {number}: {auid}"
                )
            previous = auid
            yield number, record, line


def scan_index(path):
    digest = hashlib.sha256()
    count, first, last = 0, None, None
    for _, record, line in index_rows(path):
        count += 1
        digest.update(line)
        first = first or record["auid"]
        last = record["auid"]
    return {
        "records": count,
        "sha256": digest.hexdigest(),
        "first_auid": first,
        "last_auid": last,
    }


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def material_url(aurl):
    if not isinstance(aurl, str) or not aurl or any(c.isspace() for c in aurl):
        raise ValidationError("Invalid material AURL")
    if not aurl.startswith(("https://", "http://")):
        host, separator, path = aurl.partition(":")
        if not separator or not host or not path:
            raise ValidationError(f"Invalid material AURL: {aurl!r}")
        aurl = f"https://{host}/{path.lstrip('/')}"
    parsed = urlsplit(aurl)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError(f"Invalid material AURL: {aurl!r}")
    path = quote(parsed.path.rstrip("/") + "/", safe="/:@-._~!$&'()+,;=%")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def listed_files(record):
    files = record.get("files")
    if not isinstance(files, list):
        raise ValidationError("Full material record has no valid files list")
    result, seen = [], set()
    for name in files:
        if (
            not isinstance(name, str)
            or not name
            or name in (".", "..")
            or "/" in name
            or "\\" in name
            or any(ord(c) < 32 for c in name)
        ):
            raise ValidationError(f"Unsafe or invalid listed filename: {name!r}")
        if name != "index.php" and name not in seen:
            result.append(name)
            seen.add(name)
    return result


@dataclass
class Transfer:
    bandwidth_mbps: float = 0
    delay: float = 0.3
    timeout: float = 300
    attempts: int = 5
    retry_delay: float = 5
    last_finished: float | None = None

    def download(self, url, destination, on_failure, validate=None, max_bytes=None):
        """Stream to .part, validate, and rename. A .part is never a completed file."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + ".part")
        headers = destination.with_name(destination.name + ".headers.part")
        try:
            for attempt in range(1, self.attempts + 1):
                process = None
                retry_after = 0
                try:
                    if self.last_finished is not None:
                        time.sleep(
                            max(0, self.delay - (time.monotonic() - self.last_finished))
                        )
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
                        str(min(15, self.timeout)),
                        "--user-agent",
                        "aflow-assignment-worker/1.0",
                        "--output",
                        str(part),
                        "--dump-header",
                        str(headers),
                        "--write-out",
                        "%{http_code}",
                    ]
                    if self.bandwidth_mbps:
                        command += [
                            "--limit-rate",
                            str(max(1, round(self.bandwidth_mbps * 125000))),
                        ]
                    if max_bytes is not None:
                        command += [
                            "--max-filesize",
                            str(max_bytes),
                            "--max-time",
                            str(self.timeout),
                        ]
                    else:
                        # Large simulation files may need much longer than a metadata
                        # request. Bound stalls, not the entire successful transfer.
                        minimum_rate = (
                            min(1024, max(1, round(self.bandwidth_mbps * 12500)))
                            if self.bandwidth_mbps
                            else 1024
                        )
                        command += [
                            "--speed-limit",
                            str(minimum_rate),
                            "--speed-time",
                            str(max(1, math.ceil(self.timeout))),
                        ]
                    command += ["--url", url]
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    try:
                        stdout, stderr = process.communicate(
                            timeout=self.timeout + 10 if max_bytes is not None else None
                        )
                    except subprocess.TimeoutExpired as exc:
                        raise FetchError("curl exceeded the request deadline") from exc
                    status = int(stdout.strip()) if stdout.strip().isdigit() else 0
                    retry_after = (
                        retry_after_seconds(headers) if headers.exists() else 0
                    )
                    if process.returncode or status != 200:
                        raise FetchError(
                            f"HTTP {status or 'unavailable'}, curl exit {process.returncode}: {stderr.strip()[:500]}",
                            status=status or None,
                            curl_code=process.returncode,
                            retryable=(
                                status in (408, 425, 429, 500, 502, 503, 504)
                                or process.returncode
                                in (5, 6, 7, 16, 18, 28, 35, 52, 55, 56, 92)
                            ),
                        )
                    if max_bytes is not None and part.stat().st_size > max_bytes:
                        raise ValidationError(
                            "Response exceeded the metadata size limit"
                        )
                    result = validate(part) if validate is not None else None
                    # Flush content before publishing the complete filename.
                    with part.open("rb") as stream:
                        os.fsync(stream.fileno())
                    os.replace(part, destination)
                    return result
                except (FetchError, ValidationError) as exc:
                    on_failure(attempt, exc)
                    if (
                        not isinstance(exc, FetchError)
                        or not exc.retryable
                        or attempt == self.attempts
                    ):
                        raise
                    wait = max(
                        retry_after,
                        min(60, self.retry_delay * 2 ** min(attempt - 1, 30)),
                    )
                    print(
                        f"  Attempt {attempt} failed: {exc}; retrying in {wait:g}s",
                        flush=True,
                    )
                    time.sleep(wait)
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.communicate(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.communicate()
                    self.last_finished = time.monotonic()
        finally:
            headers.unlink(missing_ok=True)


def validate_transfer_args(parser, args):
    for name in ("bandwidth_mbps", "delay", "timeout", "retry_delay"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or name == "timeout" and value == 0:
            parser.error(f"--{name.replace('_', '-')} has an invalid value")
    if args.attempts <= 0 or args.max_records < 0:
        parser.error("--attempts must be positive and --max-records nonnegative")
