# AFLOW downloads

**Get the index → split it five ways → each person downloads their part.**

Run everything from the project folder. You need Python, curl, and uv.

## 1. Get the master index — one person ====> DONE so do NOT do this, jump to step 3

```bash
python3 download_index.py --output-dir data/master_index --timeout 300 --attempts 10 --retry-delay 15 --delay 1
```

This collects only `auid` and `aurl` for 3 million entries in the DB.
It saves progress in SQLite and creates `data/master_index/master_index.jsonl` when finished.

Recovery command to recover failed entries:

```bash
python3 retry_failed_index.py --output-dir data/master_index
```

## 2. Split the finished index — one person, once ====> DONE so do NOT do this, jump to step 3

Once `complete`, then:

```bash
python3 split_index.py --input data/master_index/master_index.jsonl
```

This creates five files in `data/assignments/`, with no overlapping AUIDs.
The split uses the actual unique count, not a fixed 700,000 per person.
Give each person their part file and `assignments.json` together.

## 3. Set up each account — once per person ====> PENDING

```bash
uv sync --locked
```

## 4. Download your part — each person ====> PENDING

```bash
uv run --locked python download_materials.py --input data/assignments/part_ENTER_UR_PART_FROM_TABLE.jsonl
```

**Change only the input filename. No Python editing.**

| Person | Input filename |
| --- | --- |
| 1 | `part_1.jsonl` |
| 2 | `part_2.jsonl` |
| 3 | `part_3.jsonl` |
| 4 | `part_4.jsonl` |
| 5 | `part_5.jsonl` |

Each entry: **save raw JSON → normalize → download its files → save progress**.
Results go into `data/downloads/part_N/`, bucketed by the first three AUID characters:
`abc/abc0123456789def/`. Each folder holds raw JSON, normalized outputs, and files.

Need another storage location? Add `--output-dir /your/storage/part_1`.
HPC downloads have no bandwidth cap; add `--bandwidth-mbps 5` for home Wi-Fi.

**Stop/resume:** Ctrl+C, then rerun the same command with the same input/output.
Successful work is kept. `manifest.json` shows progress; `failures.json` logs errors
and whether they were resolved. Rerunning retries unfinished material downloads.

More detail: [material download guide](MATERIAL_DOWNLOADS.md).
