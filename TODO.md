# AFLOW download plan

Agreed on 2026-10-16. This file records the current plan for future work.
The previous implementation is under `legacy-aflow-scripts/` and is reference material
only. Active scientific modules are copied into `src/aflow/`, with the mapping in
`lib/field_map.yaml` and dependency setup in the project root. Current scripts
must not import or load files from the archive.
The master-index downloader is implemented in `download_index.py`, with targeted
failed-page recovery in `retry_failed_index.py`; see `README.md` for the complete
workflow. `split_index.py` and `download_materials.py` implement the subsequent
five-account assignments and bucketed material download/normalization workflow;
see `MATERIAL_DOWNLOADS.md`. They are ready to use after the master is complete.
Storage quotas and file-size balancing are outside the current planning scope.
The folder move into `master_index_download/` is deferred at the user's request;
the scripts and current checkpoint paths stay in place. Per-entry normalization
is confirmed: save raw JSON → normalize → download associated files → checkpoint.

## Implementation status

- [x] Implement `download_index.py` with SQLite page checkpoints, retries,
  validation, JSON failure reports, a final sorted JSONL export, and a configurable
  average transfer limit (5 Mbps by default).
- [x] Pass eleven offline integration tests, including interrupted-transfer retries,
  resume, duplicate deduplication, count/ordering checks, checkpoint migration,
  and locking.
- [x] Verify live collection through page 4 and handle identical source duplicates.
  Collection has continued since that check; consult `data/master_index/manifest.json`
  for live progress rather than the historical test counts in this plan.
- [x] Implement a streaming, immutable five-way splitter with actual AUID boundaries
  and per-assignment checksums.
- [x] Implement a common assigned-index worker with AUID prefix buckets, per-entry
  active normalization, separate artifact checkpoints, retry/resume, and failure logs.
- [x] Add master-index-only recovery of unresolved failed pages, using existing
  checkpoint identity, validation, locking, and failure-resolution history.
- [x] Document the complete workflow and exact per-person input filename commands
  in the root README.
- [x] Verify 31 offline tests across the index collector and material pipeline,
  including generated-assignment ownership, real normalization, missing-file
  recovery, and large-file transfer settings. No full material jobs were launched.
- [ ] Finish collection by running `python3 download_index.py` from the project
  directory. The full export is published only on completion.
- [ ] Split the completed export and distribute one assignment to each account.
- [ ] Launch one worker for each account's assignment.

## Agreed workflow

**Master index (`auid`, `aurl`) → deduplicate and validate → divide into five
assignments → assign to people/accounts → download and normalize each entry →
save progress and log failures.**

“Deduplicate” is intentional: remove repeated AUIDs before assigning work.

- [ ] Collect a master index using paginated queries requesting AUID and AURL.
  Store only these two fields. Save collection progress so an interrupted index
  collection can resume. The delay belongs between page requests, not between
  the individual entries returned in a page.
- [ ] Confirm the intended catalog coverage and total number of entries. The
  working estimate is 3.9 million, but the small live probes described below
  reported 2,981,853 matches. Reconcile this difference before treating a
  collection as the complete target dataset.
- [ ] Deduplicate by AUID, flag conflicting AURLs for the same AUID, sort by AUID,
  and freeze the completed master index before dividing it. Keep the collection
  query, timestamps, counts, and a checksum with the saved index.
- [ ] Split the frozen index into five non-overlapping assignments. Verify that
  their union equals the master index and that every AUID occurs exactly once
  across the assignments. Generate assignments once and reuse them for restarts.
- [ ] Give each person/account its own assignment file, including the first and
  last AUID and the assigned entry count. The same download/normalization program
  will process each assignment independently.
- [ ] For each assigned entry, fetch and preserve its full original JSON,
  normalize the record, and download its associated files into the bucket layout
  below. Normalization can happen per entry as its full JSON becomes available.
- [ ] Track metadata download, normalization, and associated-file completion
  separately so a failed file does not require repeating successful work.
- [ ] Write a separate failure JSON for each person/account. Include the AUID,
  failed stage, URL or filename, error message, and attempt time. Retrying should
  target unresolved failures and skip verified completed work.

The collected master is the assignment authority. Live pagination has no
documented snapshot guarantee: deduplication prevents duplicate assignments but
does not, by itself, prove that a changing source was collected without omissions.
Validate coverage as well as uniqueness before freezing the master.

## Five-person ranges

These are **one-based, inclusive row positions in the frozen, AUID-sorted master
index**, not numeric values of AUIDs. Actual first/last AUID boundaries will be
recorded after the master is collected. Each range includes both endpoints.
Use the final deduplicated `manifest.json` entry count (`records`) when calculating
assignments. The API total (`api_total`) counts source rows and can include repeats.

For a master containing exactly **3,900,000 unique entries**:

| Person/account | Start row | End row | Entries |
| --- | ---: | ---: | ---: |
| 1 | 1 | 780,000 | 780,000 |
| 2 | 780,001 | 1,560,000 | 780,000 |
| 3 | 1,560,001 | 2,340,000 | 780,000 |
| 4 | 2,340,001 | 3,120,000 | 780,000 |
| 5 | 3,120,001 | 3,900,000 | 780,000 |

Calculate the real ranges from the final unique count: divide by five and give
one extra entry to each of the first people until the remainder is exhausted.
Never stop at a fixed 700,000 entries per account and leave a remainder unassigned.

If the final validated count instead equals the **2,981,853 matches reported by
the tested query**, the ranges would be:

| Person/account | Start row | End row | Entries |
| --- | ---: | ---: | ---: |
| 1 | 1 | 596,371 | 596,371 |
| 2 | 596,372 | 1,192,742 | 596,371 |
| 3 | 1,192,743 | 1,789,113 | 596,371 |
| 4 | 1,789,114 | 2,385,483 | 596,370 |
| 5 | 2,385,484 | 2,981,853 | 596,370 |

## File buckets inside each account's storage

Use the first three hexadecimal characters after `aflow:` as the bucket name.
Retain the full hexadecimal AUID suffix as the material directory name, matching
the legacy directory naming convention.

Example for `aflow:000000f0098a6204`:

`<account-data-root>/000/000000f0098a6204/<filename>`

This permits 4,096 possible prefix buckets. The assignment file determines which
account owns a material; the prefix determines its folder within that account.
Bucket occupancy varies and is not a fixed number of materials.

## Master-index timing on the local system

Small, sequential, read-only HTTP probes were made from this machine on
2026-10-16. These timing probes measured index queries only, without material-file
downloads. The downloader's later two-page live check is recorded above.

The legacy query `auid,aurl,paging(page,200)` also returned `compound`,
`spacegroup_relax`, and `Pearson_symbol_relax` as default fields. The following
query shape was tested and returned exactly `auid` and `aurl`:

`https://aflow.org/API/aflux/?auid,aurl,$compound,$spacegroup_relax,$Pearson_symbol_relax,paging(PAGE,PAGE_SIZE)`

The dollar-prefixed fields mute defaults. Preserve those dollar signs if the
query is later used in a shell command.

| Page size | Page number | Actual rows returned | HTTP request time |
| ---: | ---: | ---: | ---: |
| 200 | 2 | 200 | 7.372 seconds |
| 200 | 5,000 | 200 | 12.039 seconds |
| 200 | 14,000 | 200 | 24.064 seconds |
| 5,000 | 1 | 5,000 | 8.510 seconds |
| 5,000 | 500 | 5,000 | 19.524 seconds |

All five responses were HTTP 200, valid JSON, contained only AUID/AURL fields,
and reported 2,981,853 total matches. An earlier unmuted 200-row probe took
10.705 seconds; it is excluded from the two-field averages below.

Estimate for one sequential collector:

`requests = ceil(unique entry count / page size)`

`time ≈ requests × (mean HTTP request time + 0.3 seconds)`

This approximation excludes substantial retry/backoff, outages, final validation,
and sorting. Server load and later-page performance can change. It is an
extrapolation from a small sample, not a completed full-run benchmark.

| Target entries | Page size | Requests | Pause time alone | Projection using sample mean |
| ---: | ---: | ---: | ---: | ---: |
| 3,900,000 | 200 | 19,500 | 97.5 minutes | About 80.1 hours |
| 3,900,000 | 5,000 | 780 | 3.9 minutes | About 3.1 hours |
| 2,981,853 | 5,000 | 597 | 3.0 minutes | About 2.4 hours |

For planning, allow **roughly 3–5 hours for the 3.9-million target using 5,000-row
pages**, assuming service behavior comparable to these probes. At the tested
query's smaller count, allow roughly 2–4 hours. These are rough working estimates,
not upper bounds. Larger pages substantially reduced the number of requests in
this test; 5,000 rows were accepted and returned in full.

The old 97.5-minute estimate was only the deliberate pauses at 200 rows per page,
not total collection time. The legacy downloader also fetches full records and
has a delay inside that per-material loop; it is not an index-only collector.

## References for future implementation

- Existing downloader: `legacy-aflow-scripts/lib/alfow_query.py`.
- Active normalization wrapper: `src/aflow/aflow.py`.
- Active per-row normalization: `src/aflow/normalize_reported_values.py`.
- Active mapping: `lib/field_map.yaml`.
- [Official AFLUX documentation](https://aflow.org/documentation/): pagination,
  configurable page size, output muting, and count-only queries.
- [Official AFLOW database/API slides](https://aflow.org/aflow-school/past_schools/20220921/03_aflow_school_database.pdf):
  ordering by the first property and ascending order for positive page numbers.

The HPC launcher and scheduler details remain unspecified. They can be added
when implementation is requested; account assignment is independent of them.
