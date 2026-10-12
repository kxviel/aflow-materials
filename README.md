# Goal:
Prepare AFLOW materials data for integration into our database.

# Description:
Review AFLOW’s structure and available properties, collect and normalize its records, validate data quality, and present database statistics and the processing workflow in a short slide deck.


how much info
what is aflo
data structure theirs(Of the 175 production properties, 171 have a documented request syntax.)
gpl3 licesnce
how to download
what is raw, what can be normalized, what can be derived
how many are experimental or practical or computational (wet lab) or experimentally proven


curl -fL --retry 3 \
  "https://aflow.org/API/aflux/?$(cat data/aflux_query.txt)" \
  -o data/aflow_data.json

  curl -fL --retry 3 \
        'https://aflow.org/API/aflux/?help(properties)' \
        -o aflow_properties.txt

url = "https://aflow.org/API/aflux/?auid,aurl,compound,paging(0)"

## Current AFLOW JSON pipeline

Run `.venv/bin/aflow` from the project directory. The default input is
`data/aflow_full_200.json`; the run writes its intermediary and normalized JSON files.

The intermediary preserves the entire original record. The merged output uses
AFLOW's property names and includes `quality`, `provenance`, and `units` metadata.
Reported values take precedence over computed replacements. Missing reported
values may use derived values, with their origin recorded per field. The original
value remains available in the intermediary, joined by AUID.

Documented energy quantities remain in eV/atom or eV/cell; `Egap` and the separately
reported `Egap_fit` remain in eV. Pressure, residual pressure, and Pulay stress are
converted from AFLOW's kbar to GPa (multiply by 0.1). Density uses g/cm³.
`pressure` refers to the unrelaxed cell; `pressure_residual` refers to the relaxed
cell. Per-cell energies keep AFLOW's simulation-cell basis, not a derived standard cell.
Numeric quantities must be finite; missing values become null. Stoichiometry is
checked against reported species and integer composition counts, and must sum to one.
Invalid quantities become null and produce an error issue.

Total energy, formation enthalpy, fitted gaps, and other definitions stay separate.
Matching units alone does not make calculations from different DFT setups comparable.
Properties without a documented normalization rule are retained as reported,
including the CCE fields absent from the downloaded property registry.

Quality status is `processed`, `review_required`, or `invalid`; it never claims
scientific certification. The normalizer produces `quality_status` for CSV and
intermediary JSON; the merged JSON exposes it as `quality.status`.
Existing symmetry/Pearson disagreements remain flagged.
The symmetry tolerances remain 0.1 Å primary, 0.01 Å QC, and 5° angle tolerance.
PBC assumptions, unavailable layer groups, and bonding-model warnings remain visible.
Derivation parameters are exported; unresolved records, including Rh6, require review.

# normalize_reported_values.py

General-purpose materials science value normalization pipeline.
Reads any CSV from any crystal-structure database, normalizes all reported fields, optionally derives values from crystal geometry, and writes a single output CSV with three column-prefix tiers.

---

## Contents

```
normalize_reported_values.py   Main script
field_map.yaml                 Database field-name mapping config
README.md                      This file
```

---

## Output column tiers

Every field in the output CSV is prefixed with one of three labels:

| Prefix         | Meaning                                                              |
| -------------- | -------------------------------------------------------------------- |
| `raw_*`        | Verbatim value from the source CSV — **never modified**              |
| `derived_*`    | Computed from crystal geometry using pymatgen / spglib               |
| `normalized_*` | Rules-only normalization of the reported string (no geometry needed) |

Plus consistency flags (`formula_consistency`, `spacegroup_reported_vs_derived`, …) that cross-check the tiers.

---

## Fields covered

| Domain                | What is normalized                                                                                                    |
| --------------------- | --------------------------------------------------------------------------------------------------------------------- |
| **Space group**       | Auto-detects ITA number, HM short/full, Hall, Schoenflies, verbal — resolves all to ITA integer + all other notations |
| **Crystal system**    | 7 canonical names (triclinic … cubic) plus ≈30 aliases per system                                                     |
| **Bravais lattice**   | 14 Pearson symbols (aP, mS, oF, cF, …) plus common aliases (fcc, bcc, rhombohedral, …)                                |
| **Point group**       | Schoenflies canonical + HM international; handles Oh^10, D2h, m-3m, mmm, …                                            |
| **Formula**           | Hill order, reduced, anonymous, composition JSON, chemical system, element count                                      |
| **Dimensionality**    | 0 / 1 / 2 / 3 / unknown from keyword rules                                                                            |
| **Material form**     | bulk, monolayer, bilayer, slab, nanowire, thin_film, …                                                                |
| **Layer group**       | ITA-layer number 1–80 with validation; -1 → `not_assigned`                                                            |
| **Wyckoff positions** | Parse any format → multiplicity + letter + site symmetry via spglib/pymatgen lookup                                   |

---

## Installation

```bash
conda create -n normalizer python=3.11
conda activate normalizer
conda install -c conda-forge numpy pymatgen spglib pyyaml
```

Minimum required: `pyyaml` (for config), `pymatgen` + `spglib` + `numpy` (for geometry derivation).
Without pymatgen, pass `--no-geometry` and only rules-based normalization runs.

---

## Quick start

```bash
# Minimal — rules-only normalization (no geometry):
python normalize_reported_values.py \
    --input           my_data.csv \
    --db-name         materials_project \
    --config          field_map.yaml \
    --output-dir      ./normalized \
    --no-geometry

# Full — with geometry derivation from lattice params:
python normalize_reported_values.py \
    --input           my_data.csv \
    --db-name         icsd \
    --config          field_map.yaml \
    --output-dir      ./normalized

# With Phase-1 site file for Wyckoff derivation:
python normalize_reported_values.py \
    --input           structures.csv \
    --db-name         phase1 \
    --config          field_map.yaml \
    --output-dir      ./normalized \
    --phase1-sites    structure_sites.csv \
    --workers         8
```

---

## CLI reference

| Argument         | Required | Default | Description                                           |
| ---------------- | -------- | ------- | ----------------------------------------------------- |
| `--input`        | Yes      | —       | Input CSV or `.csv.gz`                                |
| `--db-name`      | Yes      | —       | Key matching a block in `field_map.yaml`              |
| `--config`       | No       | None    | Path to `field_map.yaml`                              |
| `--output-dir`   | Yes      | —       | Directory for all output files                        |
| `--phase1-sites` | No       | None    | `structure_sites.csv` for Wyckoff/geometry derivation |
| `--no-geometry`  | No       | off     | Skip geometry derivation (faster; no pymatgen needed) |
| `--workers`      | No       | 1       | Parallel worker processes                             |
| `--batch-size`   | No       | 32      | Rows per worker batch                                 |
| `--log-every`    | No       | 5000    | Log progress every N rows                             |
| `--max-rows`     | No       | 0 (all) | Stop after N rows (for testing)                       |
| `--log-level`    | No       | INFO    | DEBUG / INFO / WARNING / ERROR                        |

---

## Output files

| File                              | Description                                                                     |
| --------------------------------- | ------------------------------------------------------------------------------- |
| `normalized_output.csv`           | One row per input row; all `raw_`, `derived_`, `normalized_` columns            |
| `normalization_issues.csv`        | One row per warning/error with `issue_code`, `severity`, `raw_value`, `message` |
| `normalization_issue_summary.csv` | Issue code counts (for quick QC)                                                |
| `run_manifest.json`               | Full audit trail: versions, field mapping, policy, elapsed time                 |

---

## Config file (`field_map.yaml`)

The config maps your CSV's actual column names to the canonical field names the script understands.

### Adding a new database

1. Open `field_map.yaml`
2. Copy the `my_new_database` template block
3. Rename the key to match the string you will pass to `--db-name`
4. Set each canonical field to the matching CSV column header (case-sensitive)
5. Set unused fields to `null`

```yaml
databases:
  my_lab_database:
    material_id: "sample_id"
    spacegroup: "SG_symbol"
    formula: "chemical_formula"
    lattice_a: "a_Ang"
    lattice_b: "b_Ang"
    lattice_c: "c_Ang"
    lattice_alpha: "alpha_deg"
    lattice_beta: "beta_deg"
    lattice_gamma: "gamma_deg"
    # everything else null or omitted → fuzzy auto-detection
```

Fields you omit entirely fall back to **fuzzy auto-detection**: the script checks your CSV headers against a list of ~8 common aliases per canonical field.

### Pre-configured databases

The config ships with mappings for:

- `materials_project` — MP API / MPRester flat CSV
- `icsd` — ICSD web export
- `aflow` — AFLOW REST API
- `oqmd` — OQMD flat export
- `jarvis` — JARVIS-DFT
- `c2db` — C2DB (2D materials)
- `cod` — Crystallography Open Database
- `alexandria` — Alexandria / Alexandria-3D
- `phase1` — Output of `normalize_structure_phase1_mp_compatible.py`

---

## Space group auto-detection

The script detects notation type in this priority order:

| Priority | Notation      | Example                     | Detection rule                                          |
| -------- | ------------- | --------------------------- | ------------------------------------------------------- |
| 1        | `ita_number`  | `227`                       | Pure integer 1–230                                      |
| 2        | `schoenflies` | `Oh^10`, `D2h^28`           | Regex: class letter + superscript number                |
| 3        | `hm_full`     | `Fd-3m:2`, `R-3m:R`         | Contains `:` + setting choice marker                    |
| 4        | `hall`        | `-F 4vw 2vw 3`              | Starts with lattice letter + space + screw/glide tokens |
| 5        | `hm_short`    | `Fd-3m`, `F d -3 m`, `Fd3m` | Lattice letter + HM body                                |
| 6        | `verbal`      | `cubic`, `monoclinic P`     | Matches crystal system alias list                       |
| 7        | `unknown`     | anything else               | Warning issued; `normalized_spacegroup_ita = ''`        |

All resolved notations are written as additional columns:
`normalized_spacegroup_hm_short`, `normalized_spacegroup_hm_full`, `normalized_spacegroup_hall`, `normalized_spacegroup_schoenflies`.

---

## Wyckoff position normalization

**Input formats handled:**

| Format                  | Example     |
| ----------------------- | ----------- |
| Space-separated         | `4a 8c 16d` |
| Comma-separated         | `4a,8c,16d` |
| Semicolon-separated     | `4a;8c;16d` |
| Bracket-wrapped         | `[4a, 8c]`  |
| Reversed (letter first) | `a4,c8`     |

**Output columns:**

| Column                               | Example                                                         |
| ------------------------------------ | --------------------------------------------------------------- |
| `normalized_wyckoff_labels`          | `4a 8c 16d`                                                     |
| `normalized_wyckoff_multiplicities`  | `4 8 16`                                                        |
| `normalized_wyckoff_site_symmetries` | `-43m 3m. .3m`                                                  |
| `normalized_wyckoff_status`          | `ok` / `partial` / `lookup_failed` / `no_spacegroup_for_lookup` |

Wyckoff lookup requires a resolved space group ITA number (from either `normalized_spacegroup_ita` or `derived_spacegroup_ita`). If neither is available, labels and multiplicities are carried through from the parsed raw string.

---

## Consistency flags

| Column                               | Values                                                                         | Meaning                                                       |
| ------------------------------------ | ------------------------------------------------------------------------------ | ------------------------------------------------------------- |
| `formula_consistency`                | `consistent` / `mismatch` / `reported_only` / `derived_only` / `not_available` | Reported reduced formula vs. geometry-derived reduced formula |
| `spacegroup_reported_vs_derived`     | `consistent` / `conflict` / `reported_only` / `derived_only` / `not_available` | Normalized ITA number vs. derived ITA number                  |
| `crystal_system_reported_vs_derived` | same                                                                           | Normalized crystal system vs. derived crystal system          |
| `symmetry_tolerance_sensitive`       | `yes` / `no` / ``                                                              | Whether SG differs between symprec=0.1 Å and symprec=0.01 Å   |

---

## Issue codes

| Code                           | Severity | Meaning                                           |
| ------------------------------ | -------- | ------------------------------------------------- |
| `SPACEGROUP_RESOLUTION_FAILED` | warning  | Could not map reported SG to ITA number           |
| `CRYSTAL_SYSTEM_UNRECOGNIZED`  | warning  | Crystal system string not in alias table          |
| `BRAVAIS_UNRECOGNIZED`         | warning  | Bravais lattice string not recognized             |
| `POINT_GROUP_UNRECOGNIZED`     | warning  | Point group not recognized                        |
| `FORMULA_PARSE_FAILED`         | warning  | pymatgen could not parse the formula string       |
| `LAYER_GROUP_OUT_OF_RANGE`     | warning  | Layer group number outside 1–80                   |
| `LAYER_GROUP_PARSE_FAILED`     | warning  | Could not extract a number from layer group field |
| `WYCKOFF_PARTIAL_LOOKUP`       | warning  | Some Wyckoff letters not found in SG table        |
| `WYCKOFF_LOOKUP_FAILED`        | warning  | spglib/pymatgen Wyckoff lookup raised exception   |
| `REPORTED_DERIVED_SG_MISMATCH` | warning  | Normalized ITA ≠ derived ITA                      |
| `ROW_NORMALIZATION_CRASHED`    | error    | Unhandled exception during row processing         |

---

## Design principles

- **Raw values are never modified.** Every raw_ column is a verbatim copy.
- **Reported and derived values are always separate columns.** Never overwritten.
- **No geometry idealization.** `get_refined_structure()` is not called.
- **Failure is non-fatal by default.** On parse failure: `normalized_* = ''`, warning row written, processing continues.
- **Reference table built once at startup.** Space group lookup is O(1) per row.
- **Config-driven + fuzzy fallback.** Works on any CSV without pre-processing.
- **Multiprocessing-safe.** No shared mutable state; all reference data is read-only after init.

---

## Extending the script

### Adding a new notation type for space groups

Add a detection branch in `detect_spacegroup_notation()` and a resolution branch in `resolve_spacegroup_to_ita()`.

### Adding a new canonical field

1. Add the field name to `CANONICAL_FIELDS` list.
2. Add raw extraction in `normalize_row()` under the RAW VALUES section.
3. Add normalization logic under NORMALIZED section.
4. Add the canonical key and aliases to `_FUZZY_ALIASES`.
5. Add example mappings in `field_map.yaml`.

### Adding a new database config

Copy the `my_new_database` template in `field_map.yaml` and fill in column names.

---

## Relationship to normalize_structure_phase1_mp_compatible.py

| Script                                        | Role                                                                                                                                        |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `normalize_structure_phase1_mp_compatible.py` | Reads Phase-1 geometry files, derives MP-compatible symmetry and standard cells, writes geometry-centric output                             |
| `normalize_reported_values.py` (this script)  | Reads any flat CSV from any database, normalizes **reported** field values, optionally derives symmetry, writes a comparison-ready wide CSV |

The two scripts are complementary. Use `phase1` as the `--db-name` to feed Phase-1 output directly into this script and add a normalized-reported-value tier on top of what Phase-1 already derived.
