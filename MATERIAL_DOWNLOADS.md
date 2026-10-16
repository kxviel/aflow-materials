# Five-account material downloads

See [README.md](README.md) for the complete workflow, including master-index
collection, failed-index recovery, and the exact command for each of the five people.

The workflow is:

**Completed master index → five verified assignment files → the same worker for
each person → bucketed material files and per-record normalization.**

The running master-index download is independent of these commands. Wait for
its final `master_index.jsonl` and `manifest.json` status `complete` before splitting.

## 1. Split the finished index once

From the project directory:

```bash
python3 split_index.py --input data/master_index/master_index.jsonl
```

This produces:

```text
data/assignments/
├── assignments.json
├── part_1.jsonl
├── part_2.jsonl
├── part_3.jsonl
├── part_4.jsonl
└── part_5.jsonl
```

The splitter validates the completed master/checksum, counts the actual unique
entries, and divides them into five contiguous AUID ranges. Counts differ by at
most one; all entries, including the remainder, are assigned exactly once. It
streams the input rather than loading millions of records into memory. Its
manifest includes each file's checksum, record count, source row range, and actual
first/last AUID. Source rows here mean positions in the **deduplicated master**.

Assignment output is staged and published only after both input passes match.
An existing assignment directory is never overwritten. Preserve and distribute
these files instead of regenerating assignments after downloads begin.

Give each person one `part_N.jsonl`. Keep `assignments.json` alongside the part
when possible, so the worker can check its checksum and boundaries. A standalone
copied part is supported; its own checksum is frozen in the worker checkpoint.

## 2. Run the same worker with a different input

On this machine, the existing normalization environment is ready:

```bash
.venv/bin/python download_materials.py --input data/assignments/part_1.jsonl
```

Person 2 uses the same command with `part_2.jsonl`, and so on. Nobody edits the
Python source. The default output follows the input name:

| Input | Default output |
| --- | --- |
| `part_1.jsonl` | `data/downloads/part_1/` |
| `part_2.jsonl` | `data/downloads/part_2/` |
| `part_3.jsonl` | `data/downloads/part_3/` |
| `part_4.jsonl` | `data/downloads/part_4/` |
| `part_5.jsonl` | `data/downloads/part_5/` |

An explicit `--output-dir /path/to/account/storage` overrides this default.
Outputs are rooted in the project directory unless overridden, even when the
worker is invoked from another working directory.

For a fresh HPC checkout/account, install the locked normalization environment once:

```bash
uv sync --locked
uv run --locked python download_materials.py --input data/assignments/part_1.jsonl
```

Keep the project scripts, `src/aflow/`, `lib/field_map.yaml`, and the root dependency
files with each checkout. The worker calls the active normalizer directly;
`legacy-aflow-scripts/` is an archive and is not used. The worker requires pymatgen,
spglib, numpy, PyYAML, and curl. The splitter itself needs only Python.

## 3. AUID buckets and material contents

For `aflow:abc0123456789def`, the layout is:

```text
data/downloads/part_1/
├── progress.sqlite3
├── manifest.json
├── failures.jsonl
├── failures.json
└── abc/
    └── abc0123456789def/
        ├── raw.json
        ├── intermediary.json
        ├── normalized.json
        └── files/
            ├── structure.cif
            ├── CONTCAR.relax
            └── ...
```

The bucket uses the first **three hexadecimal characters** after `aflow:`; the
material folder uses the entire suffix. A prefix bucket can occur in two parts,
but their full AUID assignments remain disjoint. File names stay inside their
material folder. Directory traversal names are rejected.

The worker fetches full metadata using `?format=json`, with `aflowlib.json` as
the fallback. It checks that the returned AUID matches the assignment. It uses
the full record's AURL for associated files, since that location can differ from
the index's AURL. It downloads every distinct listed filename except the
server-generated `index.php` directory listing. There is no fixed 10–15-file cap.

Original metadata is preserved as received. The intermediary preserves raw,
normalized, derived, issue, and consistency information; normalized output uses
the existing AFLOW rules and records its intermediary/raw filenames. Scientific
statuses such as `review_required` and `invalid` remain visible. They are not
treated as transient transfer errors or automatically retried as downloads.

The chosen order is **per entry**: save full raw JSON → normalize that entry →
download its associated files → checkpoint completion. Normalization uses geometry
from the metadata, so it can run before the associated files finish. A normalization
exception is logged while associated files continue downloading; raw JSON is
retained for future reprocessing.

## Resume and failures

Metadata, normalization outputs, and each associated file have separate SQLite
checkpoints. Successful files are published by atomic rename and recorded with
size, modification time, and SHA-256. Partial `.part` files are never marked
complete; an interrupted file transfer restarts that file on the next attempt.

Press Ctrl+C to stop and run the same command to resume. Completed materials are
skipped; partially completed materials reuse their successful artifacts. Use
`--verify-completed` to also recheck completed materials for missing or changed
artifacts. Checks use sizes/mtimes first and hash a file when its mtime changed.
If raw metadata is fetched again, normalization is refreshed as well.

The input checksum and normalization code/configuration/dependency fingerprint
are bound to the output checkpoint. Another assignment or changed scientific
environment cannot silently reuse that output directory. One writer per output
directory is enforced with a file lock.

- `failures.jsonl`: incremental failure-attempt event log during a run.
- `failures.json`: streamed report of all attempts and their resolution state,
  refreshed when a pass finishes, pauses, fails, or is interrupted.
- `manifest.json`: assigned/completed/remaining counts and current status,
  refreshed periodically and at exit.
- `progress.sqlite3`: authoritative artifact and failure checkpoints.

A permanent file error such as HTTP 404 is logged while other files/materials
continue. A normalization exception is logged while that material's files can
still download. Such materials remain incomplete and can be retried without
repeating successful stages. Exhausted transient connection/server failures stop
the worker to avoid running through the assignment during an outage. Rerunning
retries unfinished work and marks recovered failures resolved.

The HPC worker defaults to **no bandwidth cap**, 0.3 seconds between requests,
and five attempts with exponential backoff. For a Wi-Fi test add
`--bandwidth-mbps 5`. The independently running index collector keeps its own
5 Mbps default. Metadata requests have a 300-second deadline; associated files
use a 300-second low-speed/stall guard so large files can take longer overall.
Change these settings with the named flags when needed. This worker uses
`--max-records` rather than `--max-pages` for a short first run:

```bash
.venv/bin/python download_materials.py --input data/assignments/part_1.jsonl --max-records 2
```

## Verification

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The offline tests exercise five-way coverage and remainders, checksum protection,
assignment ownership, real normalization, prefix folders, fallback metadata,
permanent and transient file failures, resume, and missing-artifact recovery.
No full material downloads or real master-index splitting are started by tests.
