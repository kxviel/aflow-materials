"""Retry unresolved master-index page failures in an existing checkpoint.

Recovers only the logged failed page, then pauses. Use download_index.py to
continue collecting the remaining pages. Requires Python 3.10+ and curl.
"""

from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import download_index as collector

ROOT = Path(__file__).resolve().parent


def checkpoint_arguments(args):
    """Restore collection identity without requiring page size or endpoint flags."""
    database = args.output_dir / "index.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
    config = json.loads(row[0]) if row else None
    if (
        not isinstance(config, dict)
        or config.get("schema_version") not in (1, 2)
        or config.get("query") != collector.QUERY
        or not isinstance(config.get("page_size"), int)
        or config["page_size"] <= 0
        or not isinstance(config.get("endpoint"), str)
    ):
        raise collector.ValidationError("Not a supported master-index checkpoint")

    expected = args.expected_count
    manifest_path = args.output_dir / "manifest.json"
    if expected is None and manifest_path.is_file():
        # This optional guard is recorded in the collector's report, not its DB config.
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("expected_count")
        if expected is not None and (type(expected) is not int or expected <= 0):
            raise collector.ValidationError("Invalid expected_count in manifest.json")

    flags = [
        "--output-dir",
        str(args.output_dir),
        "--endpoint",
        config["endpoint"],
        "--page-size",
        str(config["page_size"]),
    ]
    for name in ("bandwidth_mbps", "delay", "timeout", "attempts", "retry_delay"):
        flags += ["--" + name.replace("_", "-"), str(getattr(args, name))]
    if expected is not None:
        flags += ["--expected-count", str(expected)]
    return collector.arguments(flags)


def run(args):
    if not (args.output_dir / "index.sqlite3").is_file():
        raise collector.ValidationError(
            "No index.sqlite3 found. Point --output-dir at the existing master-index directory."
        )
    with collector.output_lock(args.output_dir):
        settings = checkpoint_arguments(args)
        state = collector.State(args.output_dir, settings)
        try:
            pending = [
                row[0]
                for row in state.db.execute(
                    "SELECT DISTINCT page FROM failures WHERE resolved_at IS NULL ORDER BY page"
                )
            ]
            if not pending:
                print(
                    "No unresolved master-index failures. No pages downloaded.",
                    flush=True,
                )
                print(
                    "Use download_index.py to continue collection or finish its export.",
                    flush=True,
                )
                return 0

            # The collector stops at a failed page, so valid checkpoints cannot have
            # holes or multiple failed pages ahead of the sequential checkpoint.
            if pending != [state.completed_pages + 1]:
                raise collector.ValidationError(
                    "Unresolved pages do not match the next sequential checkpoint; "
                    "inspect index.sqlite3 before recovery. No pages were downloaded."
                )
            total = state.get("api_total")
            if total is not None:
                state.check_expected(total)
            page = pending[0]
            print(
                f"Retrying failed master-index page {page:,} "
                f"({settings.page_size:,} source rows per full page).",
                flush=True,
            )
            state.change_status("in_progress")
            collector.download_page(collector.Client(settings), state, page)
            if state.source_rows == state.get("api_total"):
                output = state.export()
                print(
                    f"Recovered page {page:,}; master index complete: {output}",
                    flush=True,
                )
            else:
                state.change_status("paused")
                print(
                    f"Recovered page {page:,}; its failure attempts are marked resolved. "
                    "Collection paused.",
                    flush=True,
                )
                print(
                    "Rerun download_index.py with the same --output-dir to collect "
                    f"the remaining pages, starting at {page + 1:,}.",
                    flush=True,
                )
            return 0
        except KeyboardInterrupt:
            state.change_status(
                "interrupted", "Recovery interrupted; committed pages are saved"
            )
            print("\nInterrupted. Rerun this recovery command.", file=sys.stderr)
            return 130
        except (collector.IndexErrorBase, OSError, sqlite3.Error) as exc:
            state.change_status("failed", str(exc))
            print(
                f"Recovery stopped: {exc}\nCommitted pages are saved. "
                "See failures.json and manifest.json.",
                file=sys.stderr,
            )
            return 1
        finally:
            state.db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data/master_index",
        help="existing checkpoint directory (default: project data/master_index)",
    )
    parser.add_argument(
        "--bandwidth-mbps",
        type=float,
        default=5,
        help="average transfer limit; 0 unlimited (default: 5 Mbps)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1,
        help="pause between attempts (default: 1 second)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="request deadline in seconds (default: 300)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=10,
        help="attempts for the failed page (default: 10)",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=15,
        help="initial exponential retry delay (default: 15 seconds)",
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        help="override the API source-row count guard saved in manifest.json",
    )
    args = parser.parse_args(argv)
    args.output_dir = args.output_dir.expanduser().resolve()
    try:
        return run(args)
    except (collector.IndexErrorBase, OSError, sqlite3.Error, ValueError) as exc:
        print(f"Recovery stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main())
