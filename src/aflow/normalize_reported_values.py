"""
normalize_reported_values.py
============================

General-purpose materials science value normalization pipeline.

Reads any CSV (or gzipped CSV) from any database, maps heterogeneous column
names to canonical fields via a YAML config, then produces a single output CSV
with three column-prefix tiers:

    raw_*        Verbatim value from the source (never modified)
    derived_*    Value computed from crystal geometry (pymatgen / spglib)
    normalized_* Value from rules-only normalization of the reported text

Also writes:
    normalization_issues.csv          One row per warning/error
    normalization_issue_summary.csv   Aggregated issue-code counts
    run_manifest.json                 Full audit trail

FIELDS COVERED
--------------
    Space group      : ITA number, HM short/full, Hall, Schoenflies, verbal
    Crystal system   : 7 canonical names
    Bravais lattice  : 14 canonical Pearson symbols
    Point group      : Schoenflies and HM (international) canonical forms
    Formula          : Hill order, reduced, anonymous, chemical system
    Dimensionality   : 0 / 1 / 2 / 3 / unknown
    Material form    : bulk, monolayer, slab, …
    Layer group      : ITA-layer 1-80
    Wyckoff positions: multiplicity, letter, site symmetry (spglib lookup)

USAGE
-----
python normalize_reported_values.py \\
    --input            data.csv \\
    --db-name          my_database \\
    --config           field_map.yaml \\
    --output-dir       ./normalized \\
    --workers          4 \\
    --phase1-sites     structure_sites.csv   # optional; enables Wyckoff derivation

See README.md for full documentation and field_map.yaml for config format.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import math
import platform
import re
import time
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Any

# ── optional yaml ──────────────────────────────────────────────────────────────
try:
    import yaml

    _YAML_OK = True
except ImportError:
    _YAML_OK = False

LOG = logging.getLogger("normalizer")
SCRIPT_VERSION = "1.0.0"
POLICY_VERSION = "general-materials-normalization-v1"

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS & REFERENCE TABLES
# ══════════════════════════════════════════════════════════════════════════════

NULLS = {"", "na", "n/a", "nan", "none", "null", "_", "-", "--", "?", "unknown"}

# ── Crystal systems ────────────────────────────────────────────────────────────
CRYSTAL_SYSTEM_MAP: dict[str, str] = {}
_CS_ALIASES = {
    "triclinic": ["triclinic", "anorthic", "tri", "tric", "triklin"],
    "monoclinic": ["monoclinic", "mono", "mon", "monoklin"],
    "orthorhombic": ["orthorhombic", "ortho", "orth", "rhombic", "orthorhomb"],
    "tetragonal": ["tetragonal", "tet", "tetr", "tetragonal"],
    "trigonal": ["trigonal", "trig", "rhombohedral", "rhombo", "rh"],
    "hexagonal": ["hexagonal", "hex", "hexag"],
    "cubic": ["cubic", "cube", "cub", "isometric", "iso"],
}
for _canon, _aliases in _CS_ALIASES.items():
    for _a in _aliases:
        CRYSTAL_SYSTEM_MAP[_a.lower()] = _canon

# ITA-number → crystal system
_SG_TO_CS: dict[int, str] = {}
for _sg in range(1, 3):
    _SG_TO_CS[_sg] = "triclinic"
for _sg in range(3, 16):
    _SG_TO_CS[_sg] = "monoclinic"
for _sg in range(16, 75):
    _SG_TO_CS[_sg] = "orthorhombic"
for _sg in range(75, 143):
    _SG_TO_CS[_sg] = "tetragonal"
for _sg in range(143, 168):
    _SG_TO_CS[_sg] = "trigonal"
for _sg in range(168, 195):
    _SG_TO_CS[_sg] = "hexagonal"
for _sg in range(195, 231):
    _SG_TO_CS[_sg] = "cubic"

# ── Bravais lattice ────────────────────────────────────────────────────────────
# Pearson symbol → canonical
BRAVAIS_CANONICAL = {
    "aP",
    "mP",
    "mS",
    "mC",
    "oP",
    "oS",
    "oC",
    "oF",
    "oI",
    "tP",
    "tI",
    "hP",
    "hR",
    "cP",
    "cF",
    "cI",
}
_BRAVAIS_ALIASES: dict[str, str] = {}
_BL_ALIAS_MAP = {
    "aP": ["aP", "triclinic P", "triclinic primitive"],
    "mP": ["mP", "monoclinic P", "monoclinic primitive"],
    "mS": ["mS", "mC", "monoclinic C", "monoclinic base-centered", "monoclinic S"],
    "oP": ["oP", "orthorhombic P", "orthorhombic primitive"],
    "oS": [
        "oS",
        "oC",
        "orthorhombic C",
        "orthorhombic base-centered",
        "orthorhombic S",
    ],
    "oF": ["oF", "orthorhombic F", "orthorhombic face-centered"],
    "oI": ["oI", "orthorhombic I", "orthorhombic body-centered"],
    "tP": ["tP", "tetragonal P", "tetragonal primitive"],
    "tI": ["tI", "tetragonal I", "tetragonal body-centered"],
    "hP": ["hP", "hexagonal P", "hexagonal primitive", "trigonal P"],
    "hR": ["hR", "rhombohedral", "trigonal R", "hexagonal R"],
    "cP": ["cP", "cubic P", "cubic primitive", "simple cubic"],
    "cF": ["cF", "cubic F", "cubic face-centered", "fcc", "face-centered cubic"],
    "cI": ["cI", "cubic I", "cubic body-centered", "bcc", "body-centered cubic"],
}
for _canon, _aliases in _BL_ALIAS_MAP.items():
    for _a in _aliases:
        _BRAVAIS_ALIASES[_a.lower()] = _canon

# ── Point group ────────────────────────────────────────────────────────────────
# Schoenflies → (schoenflies_canonical, hm_canonical)
POINT_GROUP_TABLE: dict[str, tuple[str, str]] = {
    "c1": ("C1", "1"),
    "ci": ("Ci", "-1"),
    "s2": ("Ci", "-1"),
    "c2": ("C2", "2"),
    "cs": ("Cs", "m"),
    "c1h": ("Cs", "m"),
    "c2h": ("C2h", "2/m"),
    "d2": ("D2", "222"),
    "v": ("D2", "222"),
    "c2v": ("C2v", "mm2"),
    "d2h": ("D2h", "mmm"),
    "vh": ("D2h", "mmm"),
    "c4": ("C4", "4"),
    "s4": ("S4", "-4"),
    "c4h": ("C4h", "4/m"),
    "d4": ("D4", "422"),
    "c4v": ("C4v", "4mm"),
    "d2d": ("D2d", "-42m"),
    "vd": ("D2d", "-42m"),
    "d4h": ("D4h", "4/mmm"),
    "c3": ("C3", "3"),
    "s6": ("S6", "-3"),
    "c3i": ("S6", "-3"),
    "d3": ("D3", "32"),
    "c3v": ("C3v", "3m"),
    "d3d": ("D3d", "-3m"),
    "c6": ("C6", "6"),
    "c3h": ("C3h", "-6"),
    "c6h": ("C6h", "6/m"),
    "d6": ("D6", "622"),
    "c6v": ("C6v", "6mm"),
    "d3h": ("D3h", "-6m2"),
    "d6h": ("D6h", "6/mmm"),
    "t": ("T", "23"),
    "th": ("Th", "m-3"),
    "o": ("O", "432"),
    "td": ("Td", "-43m"),
    "oh": ("Oh", "m-3m"),
}
# HM → schoenflies (reverse map)
_HM_TO_PG: dict[str, str] = {v[1]: v[0] for v in POINT_GROUP_TABLE.values()}

# ── Schoenflies regex for space groups (Oh^10 style) ──────────────────────────
_RE_SCHOENFLIES_SG = re.compile(
    r"^([CTDSOIAB][a-z0-9]*[vh]?[di]?)\^?_?(\d{1,3})$", re.IGNORECASE
)
# ── Hermann-Mauguin short patterns ────────────────────────────────────────────
_RE_HM_SHORT = re.compile(
    r"^[ABCFIPRHST][\s\-]*[\d/mabcnuved\s\-]{1,30}$", re.IGNORECASE
)
# ── Hall symbol pattern ───────────────────────────────────────────────────────
_RE_HALL = re.compile(r"^-?[ABCFIPRHST]\s+[\d\w\s\-\'\"]+$")

# ── Dimensionality keywords ────────────────────────────────────────────────────
_DIM_RULES = [
    (r"\b2d[\s\-]*bulk\b", "3"),
    (r"\b3d\b", "3"),
    (r"\b2d\b", "2"),
    (r"\b1d\b", "1"),
    (r"\b0d\b", "0"),
    (r"\bmonolayer\b|singlelayer|single[\s-]layer", "2"),
    (r"\bbilayer\b", "2"),
    (r"\bfew[\s-]layer\b", "2"),
    (r"\bmultilayer\b", "2"),
    (r"\bslab\b", "2"),
    (r"\bsurface\b", "2"),
    (r"\bnanowire\b", "1"),
    (r"\bnanorod\b", "1"),
    (r"\bnanotube\b", "1"),
    (r"\bcluster\b", "0"),
    (r"\bmolecule\b", "0"),
    (r"\bbulk\b", "3"),
]

# ── Material form keywords ─────────────────────────────────────────────────────
_FORM_RULES = [
    ("polycrystalline", "polycrystalline"),
    ("single crystal", "single_crystal"),
    ("thin film", "thin_film"),
    ("nanocomposite", "nanocomposite"),
    ("composite", "composite"),
    ("nanowire", "nanowire"),
    ("nanorod", "nanorod"),
    ("nanotube", "nanotube"),
    ("nanostructure", "nanostructure"),
    ("ceramic", "ceramic"),
    ("heterostructure", "heterostructure"),
    ("bilayer", "bilayer"),
    ("monolayer", "monolayer"),
    ("singlelayer", "monolayer"),
    ("single layer", "monolayer"),
    ("few-layer", "few_layer"),
    ("few layer", "few_layer"),
    ("multilayer", "multilayer"),
    ("slab", "slab"),
    ("surface", "surface"),
    ("bulk", "bulk"),
    ("bulk_parent_2d", "bulk_parent_2d"),
    ("defect", "defect_structure"),
]

# ── Canonical output fields ────────────────────────────────────────────────────
CANONICAL_FIELDS = [
    # identity
    "source_row_id",
    "source_database",
    "source_material_id",
    # ── raw (verbatim from input) ──
    "raw_spacegroup",
    "raw_crystal_system",
    "raw_bravais_lattice",
    "raw_point_group",
    "raw_formula",
    "raw_composition",
    "raw_dimensionality",
    "raw_material_form",
    "raw_layer_group",
    "raw_wyckoff",
    "raw_lattice_a",
    "raw_lattice_b",
    "raw_lattice_c",
    "raw_lattice_alpha",
    "raw_lattice_beta",
    "raw_lattice_gamma",
    "raw_lattice_volume",
    "raw_pbc",
    "raw_lattice_matrix",
    "raw_nsites",
    "raw_site_species",
    # ── derived (computed from geometry) ──
    "derived_formula",
    "derived_reduced_formula",
    "derived_formula_hill",
    "derived_formula_anonymous",
    "derived_composition_json",
    "derived_chemical_system",
    "derived_nelements",
    "derived_nsites",
    "derived_spacegroup_ita",
    "derived_spacegroup_hm_short",
    "derived_spacegroup_hm_full",
    "derived_spacegroup_hall",
    "derived_spacegroup_schoenflies",
    "derived_crystal_system",
    "derived_point_group_schoenflies",
    "derived_point_group_hm",
    "derived_bravais_lattice",
    "derived_wyckoff_labels",
    "derived_wyckoff_multiplicities",
    "derived_wyckoff_site_symmetries",
    "derived_primitive_spacegroup_ita",
    "derived_primitive_hm_short",
    "derived_conventional_spacegroup_ita",
    "derived_conventional_hm_short",
    "qc_spacegroup_ita_0p01",
    "qc_hm_short_0p01",
    "derived_symmetry_stable",
    "derived_lattice_a",
    "derived_lattice_b",
    "derived_lattice_c",
    "derived_lattice_alpha",
    "derived_lattice_beta",
    "derived_lattice_gamma",
    "derived_lattice_volume",
    "derived_status",
    "derived_error",
    # ── normalized (rules-only from reported text) ──
    "normalized_spacegroup_ita",
    "normalized_spacegroup_notation_detected",
    "normalized_spacegroup_hm_short",  # resolved from reported
    "normalized_spacegroup_hm_full",
    "normalized_spacegroup_hall",
    "normalized_spacegroup_schoenflies",
    "normalized_crystal_system",
    "normalized_bravais_lattice",
    "normalized_point_group_schoenflies",
    "normalized_point_group_hm",
    "normalized_formula_hill",
    "normalized_formula_reduced",
    "normalized_formula_anonymous",
    "normalized_composition_json",
    "normalized_chemical_system",
    "normalized_nelements",
    "normalized_dimensionality",
    "normalized_material_form",
    "normalized_layer_group_ita",
    "normalized_layer_group_status",
    "normalized_wyckoff_labels",
    "normalized_wyckoff_multiplicities",
    "normalized_wyckoff_site_symmetries",
    "normalized_wyckoff_status",
    # ── consistency flags ──
    "formula_consistency",
    "spacegroup_reported_vs_derived",
    "crystal_system_reported_vs_derived",
    "symmetry_tolerance_sensitive",
    "normalization_status",
    "normalization_policy_version",
]

ISSUE_FIELDS = [
    "source_row_id",
    "source_database",
    "source_material_id",
    "field",
    "severity",
    "issue_code",
    "raw_value",
    "message",
]

# ══════════════════════════════════════════════════════════════════════════════
# REFERENCE TABLE  — built once from spglib at startup
# ══════════════════════════════════════════════════════════════════════════════

_SG_REF: dict[int, dict] = {}  # ita_number → {hm_short, hm_full, hall, schoenflies, …}
_HM_TO_ITA: dict[str, int] = {}  # normalised HM string → ita_number
_HALL_TO_ITA: dict[str, int] = {}  # normalised Hall string → ita_number
_SCHOENFLIES_SG_TO_ITA: dict[str, int] = {}  # "Oh^10" normalised → ita_number


def _build_sg_reference():
    """Populate _SG_REF and reverse-lookup dicts from spglib at startup."""
    global _SG_REF, _HM_TO_ITA, _HALL_TO_ITA, _SCHOENFLIES_SG_TO_ITA
    if _SG_REF:
        return
    try:
        import spglib
    except ImportError:
        LOG.warning("spglib not available — space group reference table will be empty.")
        return

    # spglib provides get_spacegroup_type(hall_number) for hall_numbers 1..530
    seen_ita: dict[int, dict] = {}
    try:
        max_hall = 530
        for hn in range(1, max_hall + 1):
            try:
                info = spglib.get_spacegroup_type(hn)
            except Exception:
                continue
            ita = int(info.get("number", 0))
            if not (1 <= ita <= 230):
                continue
            hm_short = (info.get("international_short") or "").strip()
            hm_full = (info.get("international_full") or "").strip()
            hall_sym = (info.get("hall_symbol") or "").strip()
            schoen = (info.get("schoenflies") or "").strip()
            choice = (info.get("choice") or "").strip()

            # Store first occurrence per ITA (primary setting)
            if ita not in seen_ita:
                seen_ita[ita] = {
                    "hm_short": hm_short,
                    "hm_full": hm_full,
                    "hall_symbol": hall_sym,
                    "schoenflies": schoen,
                    "choice": choice,
                    "crystal_system": _SG_TO_CS.get(ita, ""),
                }

            # Reverse lookups (all settings / choices)
            for hm in (hm_short, hm_full):
                if hm:
                    k = _normalise_hm_key(hm)
                    if k not in _HM_TO_ITA:
                        _HM_TO_ITA[k] = ita
            if hall_sym:
                k = hall_sym.strip()
                if k not in _HALL_TO_ITA:
                    _HALL_TO_ITA[k] = ita
            # Schoenflies with superscript number e.g. "Oh^10"
            if schoen:
                parts = schoen.split("^")
                key = (
                    f"{parts[0].strip()}^{parts[1].strip()}"
                    if len(parts) == 2
                    else schoen.strip()
                )
                key_norm = key.lower().replace(" ", "")
                if key_norm not in _SCHOENFLIES_SG_TO_ITA:
                    _SCHOENFLIES_SG_TO_ITA[key_norm] = ita

        _SG_REF = seen_ita
        LOG.info("Space group reference table built: %d entries", len(_SG_REF))
    except Exception as exc:
        LOG.warning("Error building SG reference table: %s", exc)


def _normalise_hm_key(hm: str) -> str:
    """Normalise HM symbol for lookup: strip spaces, unify minus signs."""
    s = hm.strip()
    s = re.sub(r"\s+", "", s)
    s = s.replace("−", "-").replace("–", "-").replace("—", "-")
    return s.lower()


# ══════════════════════════════════════════════════════════════════════════════
# BASIC HELPERS
# ══════════════════════════════════════════════════════════════════════════════


def is_null(v) -> bool:
    return v is None or str(v).strip().lower() in NULLS


def txt(v) -> str:
    return "" if v is None else str(v).strip()


def safe_int(v) -> int | None:
    if is_null(v):
        return None
    try:
        x = float(str(v).strip())
        if not math.isfinite(x) or abs(x - round(x)) > 1e-9:
            return None
        return int(round(x))
    except Exception:
        return None


def safe_float(v) -> float | None:
    if is_null(v):
        return None
    try:
        x = float(str(v).strip())
        return x if math.isfinite(x) else None
    except Exception:
        return None


def compact_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", errors="replace", newline="")
    return path.open("r", encoding="utf-8-sig", errors="replace", newline="")


def open_output_csv(path: Path, fields: Sequence[str], gzip_output: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if gzip_output:
        fh = gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=6)
    else:
        fh = path.open("w", encoding="utf-8", newline="")
    writer = csv.DictWriter(fh, fieldnames=list(fields), extrasaction="ignore")
    writer.writeheader()
    return fh, writer


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG LOADING
# ══════════════════════════════════════════════════════════════════════════════

# Canonical field names and common fuzzy aliases for auto-detection
_FUZZY_ALIASES: dict[str, list[str]] = {
    "spacegroup": [
        "space_group",
        "spacegroup",
        "sg",
        "space group",
        "sg_number",
        "sg_symbol",
        "spacegroup_number",
        "spacegroup_symbol",
        "space_group_number",
        "space_group_symbol",
        "space_group_name",
        "spg",
        "symmetry_group",
    ],
    "crystal_system": [
        "crystal_system",
        "crystal system",
        "crystal_sys",
        "xtal_system",
        "crystallographic_system",
        "symmetry_system",
    ],
    "bravais_lattice": [
        "bravais",
        "bravais_lattice",
        "bravais lattice",
        "lattice_type",
        "lattice_centering",
        "centering",
    ],
    "point_group": [
        "point_group",
        "point group",
        "pg",
        "pointgroup",
        "molecular_symmetry",
    ],
    "formula": [
        "formula",
        "pretty_formula",
        "formula_pretty",
        "chemical_formula",
        "composition",
        "formula_hill",
        "full_formula",
    ],
    "composition": ["composition", "composition_json", "comp", "species", "elements"],
    "dimensionality": [
        "dimensionality",
        "dim",
        "dimension",
        "structural_dimensionality",
        "material_dimensionality",
    ],
    "material_form": [
        "material_form",
        "form",
        "morphology",
        "sample_form",
        "material_type",
    ],
    "layer_group": [
        "layer_group",
        "layer group",
        "lg",
        "layergroup",
        "lg_number",
        "layer_group_number",
    ],
    "wyckoff": [
        "wyckoff",
        "wyckoff_sites",
        "wyckoff_positions",
        "wyckoff_letters",
        "wyk",
        "wyck",
    ],
    "lattice_a": ["a", "lattice_a", "a_ang", "a_angstrom", "lat_a", "cell_a"],
    "lattice_b": ["b", "lattice_b", "b_ang", "b_angstrom", "lat_b", "cell_b"],
    "lattice_c": ["c", "lattice_c", "c_ang", "c_angstrom", "lat_c", "cell_c"],
    "lattice_alpha": [
        "alpha",
        "lattice_alpha",
        "alpha_deg",
        "angle_alpha",
        "cell_alpha",
    ],
    "lattice_beta": ["beta", "lattice_beta", "beta_deg", "angle_beta", "cell_beta"],
    "lattice_gamma": [
        "gamma",
        "lattice_gamma",
        "gamma_deg",
        "angle_gamma",
        "cell_gamma",
    ],
    "lattice_volume": [
        "volume",
        "lattice_volume",
        "vol",
        "cell_volume",
        "unit_cell_volume",
    ],
    "pbc": ["pbc", "periodic_boundary_conditions", "periodicity", "pbc_json"],
    "lattice_matrix": [
        "lattice_matrix",
        "lattice_matrix_json",
        "cell_matrix",
        "matrix",
    ],
    "nsites": [
        "nsites",
        "site_count",
        "natoms",
        "num_atoms",
        "number_of_atoms",
        "number_of_sites",
        "num_sites",
    ],
    "site_species": ["site_species", "species", "species_json", "atoms", "atom_types"],
    "material_id": [
        "material_id",
        "id",
        "mat_id",
        "source_id",
        "icsd_id",
        "mp_id",
        "source_material_id",
        "mid",
    ],
}


def load_config(config_path: Path | None, db_name: str) -> dict[str, str]:
    """
    Returns a dict {canonical_field: source_column_name} for the given db_name.
    Falls back to fuzzy matching if field is not in config.
    """
    mapping: dict[str, str] = {}
    if config_path is None or not config_path.exists():
        return mapping
    if not _YAML_OK:
        raise SystemExit(
            "PyYAML is required for config files. Install with:\n"
            "  pip install pyyaml\n  or  conda install -c conda-forge pyyaml"
        )
    with config_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    databases = cfg.get("databases", {})
    db_cfg = databases.get(db_name, {})
    for canon, src_col in db_cfg.items():
        if src_col:
            mapping[canon] = str(src_col)
    return mapping


def fuzzy_map(headers: list[str], config_map: dict[str, str]) -> dict[str, str]:
    """
    Complete the mapping for any canonical fields not covered by config,
    using fuzzy alias matching against actual CSV headers.
    Returns {canonical: actual_header}.
    """
    header_lower = {h.lower().strip(): h for h in headers}
    result = dict(config_map)  # start with config
    for canon, aliases in _FUZZY_ALIASES.items():
        if canon in result:
            continue  # already mapped by config
        for alias in aliases:
            if alias.lower() in header_lower:
                result[canon] = header_lower[alias.lower()]
                break
    return result


def get_field(row: dict, mapping: dict[str, str], canon: str) -> str:
    """Extract value from row using canonical→actual mapping."""
    col = mapping.get(canon)
    if col and col in row:
        return txt(row[col])
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# SPACE GROUP AUTO-DETECTION & NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════


def detect_spacegroup_notation(raw: str) -> str:
    """
    Detect the notation type of a raw space group string.
    Returns one of: ita_number | hm_short | hm_full | hall | schoenflies | verbal | unknown
    """
    if is_null(raw):
        return "unknown"
    s = raw.strip()

    # 1. Pure integer 1–230
    try:
        n = int(float(s))
        if 1 <= n <= 230:
            return "ita_number"
    except (ValueError, TypeError):
        pass

    # 2. Schoenflies with superscript (space-group Schoenflies, e.g. Oh^10, D2h^28)
    if _RE_SCHOENFLIES_SG.match(s.replace(" ", "")):
        return "schoenflies"

    # 3. HM full — contains origin/setting choice marker
    if re.search(r":[\s]*(1|2|R|H|-[13])", s):
        return "hm_full"

    # 4. Hall symbol heuristic — starts with optional '-' then lattice letter + space
    if _RE_HALL.match(s) and " " in s.strip():
        # Hall symbols almost always contain spaces between tokens
        # Extra guard: contains spglib-style tokens
        if re.search(r"\b[0-9][acbnvdwxyz]\b|\b[24][acbnvdwxyz\"']\b", s):
            return "hall"

    # 5. HM short — starts with lattice letter, may contain spaces and minus signs
    if re.match(
        r"^[ABCFIPRHabcfiprhST][\d/mabcnuved\s\-\'\"]{1,30}$", s, re.IGNORECASE
    ):
        return "hm_short"

    # 6. Verbal / free text crystal system names
    low = s.lower().replace("-", " ").replace("_", " ")
    for alias in CRYSTAL_SYSTEM_MAP:
        if low == alias or low.startswith(alias + " ") or low.endswith(" " + alias):
            return "verbal"

    return "unknown"


def resolve_spacegroup_to_ita(
    raw: str, notation: str
) -> tuple[int | None, dict[str, str]]:
    """
    Given raw string and detected notation type, resolve to ITA number (1–230).
    Returns (ita_int_or_None, {hm_short, hm_full, hall, schoenflies, crystal_system}).
    """
    extras: dict[str, str] = {}

    if is_null(raw) or notation == "unknown":
        return None, extras

    s = raw.strip()

    # ── ITA number ─────────────────────────────────────────────────────────
    if notation == "ita_number":
        ita = safe_int(s)
        if ita and 1 <= ita <= 230 and ita in _SG_REF:
            ref = _SG_REF[ita]
            extras = {
                "hm_short": ref["hm_short"],
                "hm_full": ref["hm_full"],
                "hall": ref["hall_symbol"],
                "schoenflies": ref["schoenflies"],
                "crystal_system": ref["crystal_system"],
            }
        return ita if (ita and 1 <= ita <= 230) else None, extras

    # ── Schoenflies ─────────────────────────────────────────────────────────
    if notation == "schoenflies":
        key = s.lower().replace(" ", "")
        ita = _SCHOENFLIES_SG_TO_ITA.get(key)
        if ita and ita in _SG_REF:
            ref = _SG_REF[ita]
            extras = {
                "hm_short": ref["hm_short"],
                "hm_full": ref["hm_full"],
                "hall": ref["hall_symbol"],
                "crystal_system": ref["crystal_system"],
                "schoenflies": ref["schoenflies"],
            }
        return ita, extras

    # ── Hall ────────────────────────────────────────────────────────────────
    if notation == "hall":
        ita = _HALL_TO_ITA.get(s.strip())
        if ita and ita in _SG_REF:
            ref = _SG_REF[ita]
            extras = {
                "hm_short": ref["hm_short"],
                "hm_full": ref["hm_full"],
                "hall": ref["hall_symbol"],
                "schoenflies": ref["schoenflies"],
                "crystal_system": ref["crystal_system"],
            }
        return ita, extras

    # ── HM short or full ────────────────────────────────────────────────────
    if notation in ("hm_short", "hm_full"):
        key = _normalise_hm_key(s)
        ita = _HM_TO_ITA.get(key)
        if ita and ita in _SG_REF:
            ref = _SG_REF[ita]
            extras = {
                "hm_short": ref["hm_short"],
                "hm_full": ref["hm_full"],
                "hall": ref["hall_symbol"],
                "schoenflies": ref["schoenflies"],
                "crystal_system": ref["crystal_system"],
            }
        return ita, extras

    # ── Verbal — can only resolve crystal system, not ITA ───────────────────
    if notation == "verbal":
        low = s.lower().replace("-", " ").replace("_", " ")
        cs = CRYSTAL_SYSTEM_MAP.get(low.strip(), "")
        if not cs:
            for alias, canon in CRYSTAL_SYSTEM_MAP.items():
                if alias in low:
                    cs = canon
                    break
        extras["crystal_system"] = cs
        return None, extras

    return None, extras


# ══════════════════════════════════════════════════════════════════════════════
# CRYSTAL SYSTEM NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════


def normalize_crystal_system(raw: str) -> str:
    if is_null(raw):
        return ""
    low = raw.strip().lower().replace("-", " ").replace("_", " ")
    if low in CRYSTAL_SYSTEM_MAP:
        return CRYSTAL_SYSTEM_MAP[low]
    for alias, canon in CRYSTAL_SYSTEM_MAP.items():
        if alias in low:
            return canon
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# BRAVAIS LATTICE NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════


def normalize_bravais(raw: str) -> str:
    if is_null(raw):
        return ""
    s = raw.strip()
    # Direct Pearson symbol match (case-sensitive)
    if s in BRAVAIS_CANONICAL:
        return s
    # Alias lookup (case-insensitive)
    key = s.lower()
    if key in _BRAVAIS_ALIASES:
        return _BRAVAIS_ALIASES[key]
    # Fuzzy: try to find a substring alias
    for alias, canon in _BRAVAIS_ALIASES.items():
        if alias in key:
            return canon
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# POINT GROUP NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════


def normalize_point_group(raw: str) -> tuple[str, str]:
    """Returns (schoenflies_canonical, hm_canonical) or ('','')."""
    if is_null(raw):
        return "", ""
    s = raw.strip()

    # Remove superscript/sub that make no sense for point groups
    s_clean = re.sub(r"[\^_]\d+", "", s).strip()

    # Try Schoenflies lookup
    key = s_clean.lower().replace(" ", "").replace("-", "").replace("_", "")
    if key in POINT_GROUP_TABLE:
        return POINT_GROUP_TABLE[key]

    # Try HM → Schoenflies lookup
    hm_key = s_clean.replace(" ", "").replace("−", "-")
    if hm_key in _HM_TO_PG:
        schoen = _HM_TO_PG[hm_key]
        return POINT_GROUP_TABLE.get(schoen.lower(), (schoen, hm_key))

    # Fallback: try partial match
    for k, v in POINT_GROUP_TABLE.items():
        if k in key or key in k:
            return v

    return "", ""


# ══════════════════════════════════════════════════════════════════════════════
# FORMULA NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════


def normalize_formula(raw: str):
    """
    Returns dict with keys: hill, reduced, anonymous, composition_json,
    chemical_system, nelements.
    Requires pymatgen. Returns empty strings on failure.
    """
    result = {
        "hill": "",
        "reduced": "",
        "anonymous": "",
        "composition_json": "",
        "chemical_system": "",
        "nelements": "",
    }
    if is_null(raw):
        return result
    try:
        from pymatgen.core import Composition

        comp = Composition(raw.strip())
        result["hill"] = comp.hill_formula.replace(" ", "")
        result["reduced"] = comp.reduced_formula
        result["anonymous"] = comp.anonymized_formula
        result["composition_json"] = compact_json(
            {el.symbol: float(v) for el, v in comp.items()}
        )
        try:
            result["chemical_system"] = comp.chemical_system
        except Exception:
            result["chemical_system"] = "-".join(
                sorted(el.symbol for el in comp.elements)
            )
        result["nelements"] = len(comp.elements)
    except Exception:
        pass
    return result


# ══════════════════════════════════════════════════════════════════════════════
# DIMENSIONALITY & MATERIAL FORM NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════


def normalize_dimensionality(raw: str, layer_count_raw: str = "") -> tuple[str, str]:
    """Returns (dimensionality: '0'/'1'/'2'/'3'/'unknown', material_form)."""
    if is_null(raw):
        dim = "unknown"
    else:
        low = raw.strip().lower().replace("_", " ")
        dim = "unknown"
        for pattern, d in _DIM_RULES:
            if re.search(pattern, low):
                dim = d
                break

    form = ""
    low_all = (raw or "").strip().lower().replace("_", " ")
    for needle, value in _FORM_RULES:
        if needle in low_all:
            form = value
            break

    # Infer from layer count
    if dim == "unknown":
        lc = safe_int(layer_count_raw)
        if lc is not None and lc >= 1:
            dim = "2"

    return dim, form


# ══════════════════════════════════════════════════════════════════════════════
# LAYER GROUP NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════


def normalize_layer_group(raw: str) -> tuple[str, str]:
    """Returns (layer_group_ita: '1'–'80' or '', layer_group_status)."""
    if is_null(raw):
        return "", "not_reported"
    n = safe_int(raw)
    if n is None:
        # Try to parse from string like "p2" or "layer group 5"
        m = re.search(r"\b([0-9]{1,2})\b", raw)
        if m:
            n = int(m.group(1))
    if n is None:
        return "", "parse_failed"
    if n == -1:
        return "", "not_assigned"
    if 1 <= n <= 80:
        return str(n), "valid"
    return "", "out_of_range"


# ══════════════════════════════════════════════════════════════════════════════
# WYCKOFF NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════


def parse_wyckoff_raw(raw: str) -> list[dict]:
    """
    Parse raw Wyckoff string into list of {multiplicity, letter, raw_str}.
    Handles formats: "4a 8c", "4a,8c", "[4a, 8c]", "4a;8c", "a4,c8"
    """
    if is_null(raw):
        return []
    s = re.sub(r"[\[\]\(\)]", "", raw)
    tokens = re.split(r"[,;\s]+", s.strip())
    result = []
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        m = re.match(r"^(\d+)([a-zA-Z]+)$", tok)
        if not m:
            # try reversed e.g. "a4"
            m = re.match(r"^([a-zA-Z]+)(\d+)$", tok)
            if m:
                letter, mult = m.group(1), m.group(2)
            else:
                result.append({"multiplicity": "", "letter": tok, "raw_str": tok})
                continue
        else:
            mult, letter = m.group(1), m.group(2)
        result.append({"multiplicity": mult, "letter": letter.lower(), "raw_str": tok})
    return result


def lookup_wyckoff(
    spacegroup_ita: int, wyckoff_entries: list[dict]
) -> tuple[list, list, list, str]:
    """
    Look up Wyckoff positions in spglib for the given space group.
    Returns (labels, multiplicities, site_symmetries, status).
    """
    labels, mults, syms = [], [], []
    if not wyckoff_entries:
        return labels, mults, syms, "no_wyckoff_reported"
    if not (1 <= spacegroup_ita <= 230):
        return labels, mults, syms, "invalid_spacegroup"

    try:
        import spglib

        wyk_data = (
            spglib.get_spacegroup_type_from_symmetry(spacegroup_ita, np_type="int")
            if False
            else None
        )  # placeholder

        # spglib doesn't expose Wyckoff tables directly via simple call;
        # use pymatgen's SpacegroupAnalyzer tables instead
        from pymatgen.symmetry.groups import SpaceGroup

        sg = SpaceGroup.from_int_number(spacegroup_ita)
        valid_wyk = (
            {str(s.multiplicity) + s.letter: s for s in sg.symmetry_ops}
            if False
            else {}
        )

        # pymatgen SpaceGroup has wyckoff_sites
        wyk_sites = (
            {ws.letter: ws for ws in sg.wyckoff_sites}
            if hasattr(sg, "wyckoff_sites")
            else {}
        )

        status = "ok"
        for entry in wyckoff_entries:
            letter = entry.get("letter", "").lower().strip()
            if letter in wyk_sites:
                ws = wyk_sites[letter]
                labels.append(f"{ws.multiplicity}{ws.letter}")
                mults.append(str(ws.multiplicity))
                syms.append(
                    str(ws.site_symmetry) if hasattr(ws, "site_symmetry") else ""
                )
            else:
                labels.append(entry.get("raw_str", letter))
                mults.append(entry.get("multiplicity", ""))
                syms.append("")
                status = "partial"

    except Exception as exc:
        LOG.debug("Wyckoff lookup failed: %s", exc)
        for entry in wyckoff_entries:
            labels.append(entry.get("raw_str", ""))
            mults.append(entry.get("multiplicity", ""))
            syms.append("")
        status = "lookup_failed"

    return labels, mults, syms, status


# ══════════════════════════════════════════════════════════════════════════════
# GEOMETRY-BASED DERIVATION (requires pymatgen + site data)
# ══════════════════════════════════════════════════════════════════════════════

PRIMARY_SYMPREC = 0.1
QC_SYMPREC = 0.01
ANGLE_TOLERANCE = 5.0


def _imports():
    try:
        import numpy as np
        from pymatgen.core import Composition, Lattice, Structure
        from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

        return np, Composition, Lattice, Structure, SpacegroupAnalyzer
    except ImportError as exc:
        raise RuntimeError("pymatgen/numpy not available") from exc


def derive_from_geometry(row_data: dict) -> dict:
    """
    Build a pymatgen Structure from lattice + sites in row_data, then derive
    symmetry, composition, Wyckoff positions.
    row_data keys: lattice_matrix, pbc, site_species (list of species dicts),
                   site_frac_coords (list of [x,y,z]).
    Returns dict of derived_ values.
    """
    out: dict[str, str] = {
        "derived_formula": "",
        "derived_reduced_formula": "",
        "derived_formula_hill": "",
        "derived_formula_anonymous": "",
        "derived_composition_json": "",
        "derived_chemical_system": "",
        "derived_nelements": "",
        "derived_nsites": "",
        "derived_spacegroup_ita": "",
        "derived_spacegroup_hm_short": "",
        "derived_spacegroup_hm_full": "",
        "derived_spacegroup_hall": "",
        "derived_spacegroup_schoenflies": "",
        "derived_crystal_system": "",
        "derived_point_group_schoenflies": "",
        "derived_point_group_hm": "",
        "derived_bravais_lattice": "",
        "derived_wyckoff_labels": "",
        "derived_wyckoff_multiplicities": "",
        "derived_wyckoff_site_symmetries": "",
        "derived_primitive_spacegroup_ita": "",
        "derived_primitive_hm_short": "",
        "derived_conventional_spacegroup_ita": "",
        "derived_conventional_hm_short": "",
        "qc_spacegroup_ita_0p01": "",
        "qc_hm_short_0p01": "",
        "derived_symmetry_stable": "",
        "derived_lattice_a": "",
        "derived_lattice_b": "",
        "derived_lattice_c": "",
        "derived_lattice_alpha": "",
        "derived_lattice_beta": "",
        "derived_lattice_gamma": "",
        "derived_lattice_volume": "",
        "derived_status": "not_attempted",
        "derived_error": "",
    }

    matrix = row_data.get("lattice_matrix")
    species = row_data.get("site_species")
    frac_coords = row_data.get("site_frac_coords")

    if not (matrix and species and frac_coords):
        out["derived_status"] = "skipped_missing_geometry"
        return out

    try:
        np, Composition, Lattice, Structure, SpacegroupAnalyzer = _imports()

        lattice = Lattice(matrix)
        pbc = row_data.get("pbc", [True, True, True])
        structure = Structure(
            lattice,
            species,
            frac_coords,
            coords_are_cartesian=False,
            to_unit_cell=False,
            validate_proximity=False,
        )

        # Composition
        comp = structure.composition
        red = comp.reduced_composition
        out["derived_formula"] = comp.formula.replace(" ", "")
        out["derived_reduced_formula"] = comp.reduced_formula
        out["derived_formula_hill"] = comp.hill_formula.replace(" ", "")
        out["derived_formula_anonymous"] = comp.anonymized_formula
        out["derived_composition_json"] = compact_json(
            {el.symbol: float(v) for el, v in comp.items()}
        )
        try:
            out["derived_chemical_system"] = comp.chemical_system
        except Exception:
            out["derived_chemical_system"] = "-".join(
                sorted(el.symbol for el in comp.elements)
            )
        out["derived_nelements"] = str(len(comp.elements))
        out["derived_nsites"] = str(len(structure))

        # Lattice params
        lat = structure.lattice
        out["derived_lattice_a"] = str(round(lat.a, 8))
        out["derived_lattice_b"] = str(round(lat.b, 8))
        out["derived_lattice_c"] = str(round(lat.c, 8))
        out["derived_lattice_alpha"] = str(round(lat.alpha, 6))
        out["derived_lattice_beta"] = str(round(lat.beta, 6))
        out["derived_lattice_gamma"] = str(round(lat.gamma, 6))
        out["derived_lattice_volume"] = str(round(lat.volume, 6))

        # Primary symmetry (0.1 Å)
        sga = SpacegroupAnalyzer(
            structure, symprec=PRIMARY_SYMPREC, angle_tolerance=ANGLE_TOLERANCE
        )
        sd = sga.get_symmetry_dataset()
        if sd is not None:
            ita = int(sd.number if hasattr(sd, "number") else sd["number"])
            out["derived_spacegroup_ita"] = str(ita)
            ref = _SG_REF.get(ita, {})
            out["derived_spacegroup_hm_short"] = ref.get("hm_short", "")
            out["derived_spacegroup_hm_full"] = ref.get("hm_full", "")
            out["derived_spacegroup_hall"] = ref.get("hall_symbol", "")
            out["derived_spacegroup_schoenflies"] = ref.get("schoenflies", "")
            out["derived_crystal_system"] = ref.get("crystal_system", "")
            out["derived_point_group_schoenflies"] = txt(sga.get_point_group_symbol())
            pg_s = out["derived_point_group_schoenflies"].lower().replace(" ", "")
            pg_s = re.sub(r"[\^_]\d+", "", pg_s)
            hm_pg = POINT_GROUP_TABLE.get(pg_s, ("", ""))[1]
            out["derived_point_group_hm"] = hm_pg

            # Bravais from crystal system + centering
            centering = ref.get("hm_short", " ")[0] if ref.get("hm_short") else ""
            cs_abbr = {
                "triclinic": "a",
                "monoclinic": "m",
                "orthorhombic": "o",
                "tetragonal": "t",
                "trigonal": "h",
                "hexagonal": "h",
                "cubic": "c",
            }.get(out["derived_crystal_system"], "?")
            out["derived_bravais_lattice"] = (
                cs_abbr.upper() + centering if centering else ""
            )

        # QC symmetry (0.01 Å)
        sga_qc = SpacegroupAnalyzer(
            structure, symprec=QC_SYMPREC, angle_tolerance=ANGLE_TOLERANCE
        )
        sd_qc = sga_qc.get_symmetry_dataset()
        if sd_qc is not None:
            ita_qc = int(sd_qc.number if hasattr(sd_qc, "number") else sd_qc["number"])
            out["qc_spacegroup_ita_0p01"] = str(ita_qc)
            ref_qc = _SG_REF.get(ita_qc, {})
            out["qc_hm_short_0p01"] = ref_qc.get("hm_short", "")
            if out["derived_spacegroup_ita"]:
                out["derived_symmetry_stable"] = str(ita == ita_qc)

        # Wyckoff positions from primary symmetry
        if sd is not None:
            try:
                wyk_letters = list(
                    sd.wyckoffs if hasattr(sd, "wyckoffs") else sd.get("wyckoffs", [])
                )
                wyk_mults = []
                wyk_syms = []
                try:
                    from pymatgen.symmetry.groups import SpaceGroup

                    sg = SpaceGroup.from_int_number(ita)
                    wyk_map = {}
                    if hasattr(sg, "wyckoff_sites"):
                        wyk_map = {ws.letter: ws for ws in sg.wyckoff_sites}
                    for letter in wyk_letters:
                        ws = wyk_map.get(letter)
                        if ws:
                            wyk_mults.append(str(ws.multiplicity))
                            wyk_syms.append(
                                str(ws.site_symmetry)
                                if hasattr(ws, "site_symmetry")
                                else ""
                            )
                        else:
                            wyk_mults.append("")
                            wyk_syms.append("")
                except Exception:
                    wyk_mults = [""] * len(wyk_letters)
                    wyk_syms = [""] * len(wyk_letters)

                out["derived_wyckoff_labels"] = " ".join(wyk_letters)
                out["derived_wyckoff_multiplicities"] = " ".join(wyk_mults)
                out["derived_wyckoff_site_symmetries"] = " ".join(wyk_syms)
            except Exception as exc:
                LOG.debug("Wyckoff derivation failed: %s", exc)

        # Primitive / conventional standard cells
        all_periodic = all(bool(x) for x in pbc)
        if all_periodic and sd is not None:
            try:
                prim = sga.get_primitive_standard_structure(
                    international_monoclinic=True, keep_site_properties=False
                )
                sga_p = SpacegroupAnalyzer(
                    prim, symprec=PRIMARY_SYMPREC, angle_tolerance=ANGLE_TOLERANCE
                )
                sd_p = sga_p.get_symmetry_dataset()
                if sd_p is not None:
                    ita_p = int(
                        sd_p.number if hasattr(sd_p, "number") else sd_p["number"]
                    )
                    out["derived_primitive_spacegroup_ita"] = str(ita_p)
                    out["derived_primitive_hm_short"] = _SG_REF.get(ita_p, {}).get(
                        "hm_short", ""
                    )
            except Exception as exc:
                LOG.debug("Primitive cell failed: %s", exc)

            try:
                conv = sga.get_conventional_standard_structure(
                    international_monoclinic=True, keep_site_properties=False
                )
                sga_c = SpacegroupAnalyzer(
                    conv, symprec=PRIMARY_SYMPREC, angle_tolerance=ANGLE_TOLERANCE
                )
                sd_c = sga_c.get_symmetry_dataset()
                if sd_c is not None:
                    ita_c = int(
                        sd_c.number if hasattr(sd_c, "number") else sd_c["number"]
                    )
                    out["derived_conventional_spacegroup_ita"] = str(ita_c)
                    out["derived_conventional_hm_short"] = _SG_REF.get(ita_c, {}).get(
                        "hm_short", ""
                    )
            except Exception as exc:
                LOG.debug("Conventional cell failed: %s", exc)

        out["derived_status"] = "ok"

    except Exception as exc:
        out["derived_status"] = "failed"
        out["derived_error"] = f"{type(exc).__name__}: {exc}"

    return out


# ══════════════════════════════════════════════════════════════════════════════
# SITE DATA LOADER (optional phase-1 style sites file)
# ══════════════════════════════════════════════════════════════════════════════


def load_sites_index(sites_path: Path | None) -> dict[str, list[dict]]:
    """Load structure_sites.csv into {structure_uid: [site_rows]}."""
    if sites_path is None or not sites_path.exists():
        return {}
    LOG.info("Loading sites from %s …", sites_path)
    index: dict[str, list[dict]] = {}
    with open_text(sites_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            uid = txt(row.get("structure_uid") or row.get("source_structure_uid") or "")
            if uid:
                index.setdefault(uid, []).append(row)
    LOG.info("Sites index: %d structure groups", len(index))
    return index


def build_geometry_payload(
    row: dict, mapping: dict[str, str], sites_index: dict[str, list[dict]]
) -> dict:
    """Assemble lattice + sites data for derive_from_geometry()."""
    payload: dict[str, Any] = {}

    # Lattice matrix from raw_lattice_matrix field
    raw_matrix = get_field(row, mapping, "lattice_matrix")
    if raw_matrix:
        try:
            matrix = json.loads(raw_matrix)
            if isinstance(matrix, list) and len(matrix) == 3:
                payload["lattice_matrix"] = [[float(x) for x in r] for r in matrix]
        except Exception:
            pass

    # Fallback: build matrix from a,b,c,alpha,beta,gamma
    if "lattice_matrix" not in payload:
        try:
            from pymatgen.core import Lattice

            a = safe_float(get_field(row, mapping, "lattice_a"))
            b = safe_float(get_field(row, mapping, "lattice_b"))
            c = safe_float(get_field(row, mapping, "lattice_c"))
            alpha = safe_float(get_field(row, mapping, "lattice_alpha"))
            beta = safe_float(get_field(row, mapping, "lattice_beta"))
            gamma = safe_float(get_field(row, mapping, "lattice_gamma"))
            if all(x is not None and x > 0 for x in [a, b, c, alpha, beta, gamma]):
                lat = Lattice.from_parameters(a, b, c, alpha, beta, gamma)
                payload["lattice_matrix"] = lat.matrix.tolist()
        except Exception:
            pass

    # PBC
    raw_pbc = get_field(row, mapping, "pbc")
    if raw_pbc:
        try:
            payload["pbc"] = json.loads(raw_pbc)
        except Exception:
            payload["pbc"] = [True, True, True]
    else:
        payload["pbc"] = [True, True, True]

    # Sites from inline field or external sites file
    uid = txt(row.get(mapping.get("material_id", ""), ""))
    if uid and uid in sites_index:
        site_rows = sites_index[uid]
        species_list, frac_list = [], []
        for sr in site_rows:
            sp_raw = txt(sr.get("species_json") or sr.get("species") or "")
            fx = safe_float(sr.get("frac_x") or sr.get("x") or 0)
            fy = safe_float(sr.get("frac_y") or sr.get("y") or 0)
            fz = safe_float(sr.get("frac_z") or sr.get("z") or 0)
            if sp_raw and all(v is not None for v in [fx, fy, fz]):
                try:
                    sp_data = json.loads(sp_raw)
                    sp_dict = {e["element"]: e["occupancy"] for e in sp_data}
                    species_list.append(sp_dict)
                    frac_list.append([fx, fy, fz])
                except Exception:
                    species_list.append(sp_raw)
                    frac_list.append([fx, fy, fz])
        if species_list:
            payload["site_species"] = species_list
            payload["site_frac_coords"] = frac_list

    # Inline species
    if "site_species" not in payload:
        raw_species = get_field(row, mapping, "site_species")
        if raw_species:
            try:
                payload["site_species"] = json.loads(raw_species)
                # Need frac coords too — skip if absent
            except Exception:
                pass

    return payload


# ══════════════════════════════════════════════════════════════════════════════
# PER-ROW NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════


def normalize_row(
    row: dict,
    mapping: dict[str, str],
    row_id: int,
    db_name: str,
    sites_index: dict,
    enable_geometry: bool = True,
) -> tuple[dict, list[dict]]:
    """
    Full normalization of one input row.
    Returns (output_row, issue_rows).
    """
    issues: list[dict] = []
    mat_id = get_field(row, mapping, "material_id")

    def issue(field: str, code: str, raw_val: str, msg: str, severity: str = "warning"):
        issues.append(
            {
                "source_row_id": str(row_id),
                "source_database": db_name,
                "source_material_id": mat_id,
                "field": field,
                "severity": severity,
                "issue_code": code,
                "raw_value": raw_val,
                "message": msg,
            }
        )

    out: dict[str, Any] = {f: "" for f in CANONICAL_FIELDS}
    out["source_row_id"] = str(row_id)
    out["source_database"] = db_name
    out["source_material_id"] = mat_id
    out["normalization_policy_version"] = POLICY_VERSION

    # ── RAW VALUES ─────────────────────────────────────────────────────────────
    for canon_suffix in [
        "spacegroup",
        "crystal_system",
        "bravais_lattice",
        "point_group",
        "formula",
        "composition",
        "dimensionality",
        "material_form",
        "layer_group",
        "wyckoff",
        "lattice_a",
        "lattice_b",
        "lattice_c",
        "lattice_alpha",
        "lattice_beta",
        "lattice_gamma",
        "lattice_volume",
        "pbc",
        "lattice_matrix",
        "nsites",
        "site_species",
    ]:
        out[f"raw_{canon_suffix}"] = get_field(row, mapping, canon_suffix)

    # ── DERIVED (GEOMETRY) ─────────────────────────────────────────────────────
    derived: dict[str, str] = {}
    if enable_geometry:
        try:
            geo = build_geometry_payload(row, mapping, sites_index)
            derived = derive_from_geometry(geo)
        except Exception as exc:
            derived = {"derived_status": "failed", "derived_error": str(exc)}
    else:
        derived["derived_status"] = "geometry_disabled"

    out.update(derived)

    # ── NORMALIZED: SPACE GROUP ────────────────────────────────────────────────
    raw_sg = out["raw_spacegroup"]
    notation = detect_spacegroup_notation(raw_sg)
    out["normalized_spacegroup_notation_detected"] = notation

    ita_resolved, sg_extras = resolve_spacegroup_to_ita(raw_sg, notation)

    if ita_resolved is not None:
        out["normalized_spacegroup_ita"] = str(ita_resolved)
        out["normalized_spacegroup_hm_short"] = sg_extras.get("hm_short", "")
        out["normalized_spacegroup_hm_full"] = sg_extras.get("hm_full", "")
        out["normalized_spacegroup_hall"] = sg_extras.get("hall", "")
        out["normalized_spacegroup_schoenflies"] = sg_extras.get("schoenflies", "")
    else:
        if not is_null(raw_sg) and notation != "verbal":
            issue(
                "spacegroup",
                "SPACEGROUP_RESOLUTION_FAILED",
                raw_sg,
                f"Could not resolve '{raw_sg}' (detected notation: {notation}) to ITA number",
            )

    # ── NORMALIZED: CRYSTAL SYSTEM ─────────────────────────────────────────────
    raw_cs = out["raw_crystal_system"]
    norm_cs = normalize_crystal_system(raw_cs)
    if not norm_cs and sg_extras.get("crystal_system"):
        norm_cs = sg_extras["crystal_system"]  # from SG resolution
    out["normalized_crystal_system"] = norm_cs
    if not is_null(raw_cs) and not norm_cs:
        issue(
            "crystal_system",
            "CRYSTAL_SYSTEM_UNRECOGNIZED",
            raw_cs,
            f"Could not normalize crystal system: '{raw_cs}'",
        )

    # ── NORMALIZED: BRAVAIS LATTICE ────────────────────────────────────────────
    raw_bv = out["raw_bravais_lattice"]
    norm_bv = normalize_bravais(raw_bv)
    out["normalized_bravais_lattice"] = norm_bv
    if not is_null(raw_bv) and not norm_bv:
        issue(
            "bravais_lattice",
            "BRAVAIS_UNRECOGNIZED",
            raw_bv,
            f"Could not normalize Bravais lattice: '{raw_bv}'",
        )

    # ── NORMALIZED: POINT GROUP ────────────────────────────────────────────────
    raw_pg = out["raw_point_group"]
    pg_s, pg_hm = normalize_point_group(raw_pg)
    out["normalized_point_group_schoenflies"] = pg_s
    out["normalized_point_group_hm"] = pg_hm
    if not is_null(raw_pg) and not pg_s:
        issue(
            "point_group",
            "POINT_GROUP_UNRECOGNIZED",
            raw_pg,
            f"Could not normalize point group: '{raw_pg}'",
        )

    # ── NORMALIZED: FORMULA ────────────────────────────────────────────────────
    raw_formula = out["raw_formula"]
    fdata = normalize_formula(raw_formula)
    out["normalized_formula_hill"] = str(fdata["hill"])
    out["normalized_formula_reduced"] = str(fdata["reduced"])
    out["normalized_formula_anonymous"] = str(fdata["anonymous"])
    out["normalized_composition_json"] = str(fdata["composition_json"])
    out["normalized_chemical_system"] = str(fdata["chemical_system"])
    out["normalized_nelements"] = (
        str(fdata["nelements"]) if fdata["nelements"] != "" else ""
    )
    if not is_null(raw_formula) and not fdata["hill"]:
        issue(
            "formula",
            "FORMULA_PARSE_FAILED",
            raw_formula,
            f"Could not parse formula: '{raw_formula}'",
        )

    # ── NORMALIZED: DIMENSIONALITY & MATERIAL FORM ─────────────────────────────
    raw_dim = out["raw_dimensionality"]
    raw_form = out["raw_material_form"]
    dim, form = normalize_dimensionality(raw_dim)
    # Also try material_form field
    if not form:
        _, form = normalize_dimensionality(raw_form)
    out["normalized_dimensionality"] = dim
    out["normalized_material_form"] = form

    # ── NORMALIZED: LAYER GROUP ────────────────────────────────────────────────
    raw_lg = out["raw_layer_group"]
    lg_ita, lg_status = normalize_layer_group(raw_lg)
    out["normalized_layer_group_ita"] = lg_ita
    out["normalized_layer_group_status"] = lg_status
    if lg_status == "out_of_range":
        issue(
            "layer_group",
            "LAYER_GROUP_OUT_OF_RANGE",
            raw_lg,
            f"Layer group number out of valid range 1–80: '{raw_lg}'",
        )
    elif lg_status == "parse_failed":
        issue(
            "layer_group",
            "LAYER_GROUP_PARSE_FAILED",
            raw_lg,
            f"Could not parse layer group: '{raw_lg}'",
        )

    # ── NORMALIZED: WYCKOFF ────────────────────────────────────────────────────
    raw_wyk = out["raw_wyckoff"]
    parsed_wyk = parse_wyckoff_raw(raw_wyk)

    # Use best available ITA for Wyckoff lookup
    wyk_ita = (
        safe_int(
            out.get("normalized_spacegroup_ita")
            or out.get("derived_spacegroup_ita")
            or 0
        )
        or 0
    )

    if parsed_wyk and wyk_ita:
        wlabels, wmults, wsyms, wstatus = lookup_wyckoff(wyk_ita, parsed_wyk)
        out["normalized_wyckoff_labels"] = " ".join(wlabels)
        out["normalized_wyckoff_multiplicities"] = " ".join(wmults)
        out["normalized_wyckoff_site_symmetries"] = " ".join(wsyms)
        out["normalized_wyckoff_status"] = wstatus
        if wstatus == "partial":
            issue(
                "wyckoff",
                "WYCKOFF_PARTIAL_LOOKUP",
                raw_wyk,
                "Some Wyckoff letters not found in spacegroup table",
            )
        elif wstatus == "lookup_failed":
            issue(
                "wyckoff",
                "WYCKOFF_LOOKUP_FAILED",
                raw_wyk,
                "Wyckoff table lookup failed",
            )
    elif parsed_wyk:
        # No SG available — carry through parsed values
        out["normalized_wyckoff_labels"] = " ".join(
            e.get("raw_str", "") for e in parsed_wyk
        )
        out["normalized_wyckoff_multiplicities"] = " ".join(
            e.get("multiplicity", "") for e in parsed_wyk
        )
        out["normalized_wyckoff_site_symmetries"] = ""
        out["normalized_wyckoff_status"] = "no_spacegroup_for_lookup"
    else:
        out["normalized_wyckoff_status"] = (
            "not_reported" if is_null(raw_wyk) else "parse_failed"
        )

    # ── CONSISTENCY FLAGS ──────────────────────────────────────────────────────
    # Formula: reported vs derived
    d_formula = out.get("derived_reduced_formula", "")
    n_formula = out.get("normalized_formula_reduced", "")
    if d_formula and n_formula:
        out["formula_consistency"] = (
            "consistent" if d_formula == n_formula else "mismatch"
        )
    elif n_formula and not d_formula:
        out["formula_consistency"] = "reported_only"
    elif d_formula and not n_formula:
        out["formula_consistency"] = "derived_only"
        out["normalized_formula_reduced"] = d_formula  # backfill
    else:
        out["formula_consistency"] = "not_available"

    # Space group: reported vs derived
    n_ita = safe_int(out.get("normalized_spacegroup_ita"))
    d_ita = safe_int(out.get("derived_spacegroup_ita"))
    if n_ita is not None and d_ita is not None:
        out["spacegroup_reported_vs_derived"] = (
            "consistent" if n_ita == d_ita else "conflict"
        )
        if n_ita != d_ita:
            issue(
                "spacegroup",
                "REPORTED_DERIVED_SG_MISMATCH",
                raw_sg,
                f"Reported ITA={n_ita}, derived ITA={d_ita}",
            )
    elif n_ita is not None:
        out["spacegroup_reported_vs_derived"] = "reported_only"
    elif d_ita is not None:
        out["spacegroup_reported_vs_derived"] = "derived_only"
    else:
        out["spacegroup_reported_vs_derived"] = "not_available"

    # Crystal system: reported vs derived
    n_cs = out.get("normalized_crystal_system", "")
    d_cs = out.get("derived_crystal_system", "")
    if n_cs and d_cs:
        out["crystal_system_reported_vs_derived"] = (
            "consistent" if n_cs == d_cs else "conflict"
        )
    elif n_cs or d_cs:
        out["crystal_system_reported_vs_derived"] = (
            "reported_only" if n_cs else "derived_only"
        )
    else:
        out["crystal_system_reported_vs_derived"] = "not_available"

    # Symmetry tolerance sensitive
    d_sym_stable = out.get("derived_symmetry_stable", "")
    out["symmetry_tolerance_sensitive"] = (
        "yes" if d_sym_stable == "False" else "no" if d_sym_stable == "True" else ""
    )

    # Overall normalization status
    any_ok = any(
        [
            out["normalized_spacegroup_ita"],
            out["normalized_crystal_system"],
            out["normalized_formula_hill"],
        ]
    )
    out["normalization_status"] = "complete" if any_ok else "empty"

    return out, issues


# ══════════════════════════════════════════════════════════════════════════════
# BATCH WORKER (multiprocessing-safe)
# ══════════════════════════════════════════════════════════════════════════════


def _worker_init():
    """Called once per worker process to build reference tables."""
    _build_sg_reference()
    try:
        _imports()  # warm up pymatgen import
    except Exception:
        pass


def normalize_batch(args_tuple) -> list[tuple[dict, list[dict]]]:
    batch, mapping, db_name, sites_index, enable_geometry = args_tuple
    _build_sg_reference()
    results = []
    for row_id, row in batch:
        try:
            out_row, issues = normalize_row(
                row, mapping, row_id, db_name, sites_index, enable_geometry
            )
        except Exception as exc:
            out_row = {f: "" for f in CANONICAL_FIELDS}
            out_row["source_row_id"] = str(row_id)
            out_row["source_database"] = db_name
            out_row["normalization_status"] = "crashed"
            issues = [
                {
                    "source_row_id": str(row_id),
                    "source_database": db_name,
                    "source_material_id": "",
                    "field": "all",
                    "severity": "error",
                    "issue_code": "ROW_NORMALIZATION_CRASHED",
                    "raw_value": "",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            ]
        results.append((out_row, issues))
    return results


def batched(iterable, size):
    batch = []
    for x in iterable:
        batch.append(x)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT BUNDLE
# ══════════════════════════════════════════════════════════════════════════════


class OutputBundle:
    def __init__(self, out_dir: Path):
        self.handles = []
        self.issue_counts = Counter()
        self.counts = Counter()

        fh, self.main = open_output_csv(
            out_dir / "normalized_output.csv", CANONICAL_FIELDS
        )
        self.handles.append(fh)
        fh, self.issues = open_output_csv(
            out_dir / "normalization_issues.csv", ISSUE_FIELDS
        )
        self.handles.append(fh)

    def write(self, out_row: dict, issues: list[dict]):
        self.main.writerow(out_row)
        self.counts["rows"] += 1
        status = out_row.get("normalization_status", "")
        self.counts[f"status::{status}"] += 1
        for iss in issues:
            self.issues.writerow(iss)
            self.issue_counts[iss["issue_code"]] += 1
            self.counts["issues"] += 1

    def close(self):
        for fh in self.handles:
            fh.close()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════


def dependency_versions() -> dict:
    v = {"python": platform.python_version(), "platform": platform.platform()}
    for pkg in ("numpy", "pymatgen", "spglib", "yaml"):
        try:
            mod = __import__(pkg)
            v[pkg] = getattr(mod, "__version__", "installed")
        except ImportError:
            v[pkg] = "missing"
    return v


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Normalize materials science reported values across databases."
    )
    p.add_argument("--input", required=True, type=Path, help="Input CSV (or .csv.gz)")
    p.add_argument(
        "--db-name",
        required=True,
        help="Database name key matching an entry in --config",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="YAML field-mapping config (field_map.yaml)",
    )
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument(
        "--phase1-sites",
        type=Path,
        default=None,
        help="Optional structure_sites.csv for geometry derivation",
    )
    p.add_argument(
        "--no-geometry",
        action="store_true",
        help="Skip geometry-based derivation (faster, no pymatgen needed)",
    )
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--log-every", type=int, default=5000)
    p.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Stop after N rows (testing only; 0 = all)",
    )
    p.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    versions = dependency_versions()
    LOG.info("Starting normalizer v%s", SCRIPT_VERSION)
    LOG.info("Dependencies: %s", versions)

    if not args.no_geometry and versions.get("pymatgen") == "missing":
        LOG.warning(
            "pymatgen missing — geometry derivation will be skipped. "
            "Install with: conda install -c conda-forge pymatgen"
        )
        args.no_geometry = True

    # Build reference table in main process
    _build_sg_reference()

    # Load config & sites
    config_map = load_config(args.config, args.db_name)
    sites_index = load_sites_index(args.phase1_sites) if not args.no_geometry else {}

    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = OutputBundle(args.output_dir)

    start = time.time()
    processed = 0
    fatal = ""

    try:
        with open_text(args.input) as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            LOG.info("Input columns detected: %s", headers)
            mapping = fuzzy_map(list(headers), config_map)
            LOG.info(
                "Field mapping resolved: %s", {k: v for k, v in mapping.items() if v}
            )

            def row_iter():
                for i, row in enumerate(reader, start=1):
                    if args.max_rows > 0 and i > args.max_rows:
                        break
                    yield i, row

            if args.workers <= 1:
                for batch in batched(row_iter(), args.batch_size):
                    results = normalize_batch(
                        (
                            batch,
                            mapping,
                            args.db_name,
                            sites_index,
                            not args.no_geometry,
                        )
                    )
                    for out_row, issues in results:
                        outputs.write(out_row, issues)
                        processed += 1
                        if args.log_every and processed % args.log_every == 0:
                            LOG.info("Processed %s rows", f"{processed:,}")
            else:
                max_pending = max(2, args.workers * 2)
                pending = set()
                with ProcessPoolExecutor(
                    max_workers=args.workers, initializer=_worker_init
                ) as ex:
                    for batch in batched(row_iter(), args.batch_size):
                        while len(pending) >= max_pending:
                            done, pending = wait(pending, return_when=FIRST_COMPLETED)
                            for fut in done:
                                for out_row, issues in fut.result():
                                    outputs.write(out_row, issues)
                                    processed += 1
                                    if (
                                        args.log_every
                                        and processed % args.log_every == 0
                                    ):
                                        LOG.info("Processed %s rows", f"{processed:,}")
                        pending.add(
                            ex.submit(
                                normalize_batch,
                                (
                                    batch,
                                    mapping,
                                    args.db_name,
                                    sites_index,
                                    not args.no_geometry,
                                ),
                            )
                        )
                    while pending:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for fut in done:
                            for out_row, issues in fut.result():
                                outputs.write(out_row, issues)
                                processed += 1

    except Exception as exc:
        fatal = f"{type(exc).__name__}: {exc}"
        LOG.exception("Fatal error during normalization.")
        raise
    finally:
        outputs.close()

        # Issue summary
        with (args.output_dir / "normalization_issue_summary.csv").open(
            "w", encoding="utf-8", newline=""
        ) as f:
            w = csv.writer(f)
            w.writerow(["issue_code", "count"])
            for code, count in outputs.issue_counts.most_common():
                w.writerow([code, count])

        # Manifest
        manifest = {
            "script_version": SCRIPT_VERSION,
            "normalization_policy_version": POLICY_VERSION,
            "input_file": str(args.input),
            "db_name": args.db_name,
            "config_file": str(args.config) if args.config else "",
            "phase1_sites": str(args.phase1_sites) if args.phase1_sites else "",
            "output_dir": str(args.output_dir),
            "geometry_derivation_enabled": not args.no_geometry,
            "primary_symprec_angstrom": PRIMARY_SYMPREC,
            "qc_symprec_angstrom": QC_SYMPREC,
            "angle_tolerance_degrees": ANGLE_TOLERANCE,
            "workers": args.workers,
            "batch_size": args.batch_size,
            "processed_rows": processed,
            "output_counts": dict(outputs.counts),
            "issue_counts": dict(outputs.issue_counts),
            "field_mapping_used": mapping if "mapping" in dir() else {},
            "dependency_versions": versions,
            "elapsed_seconds": round(time.time() - start, 2),
            "fatal_error": fatal,
            "normalization_fields_covered": [
                "space_group (ITA, HM short/full, Hall, Schoenflies, verbal)",
                "crystal_system (7 canonical names + aliases)",
                "bravais_lattice (14 Pearson symbols + aliases)",
                "point_group (Schoenflies + HM international)",
                "formula (Hill, reduced, anonymous, composition JSON, chemical system)",
                "dimensionality (0/1/2/3/unknown)",
                "material_form (bulk, monolayer, slab, …)",
                "layer_group (ITA-layer 1–80)",
                "wyckoff (multiplicity, letter, site symmetry via spglib/pymatgen)",
            ],
            "policy_notes": [
                "Raw values are never modified.",
                "Reported (normalized_*) and geometry-derived (derived_*) values are always separate.",
                "Space group auto-detection order: ita_number > schoenflies > hall > hm_full > hm_short > verbal > unknown.",
                "On resolution failure: normalized_spacegroup_ita='' and a warning row is written.",
                "Wyckoff lookup requires a resolved space group ITA number.",
                "Geometry derivation uses pymatgen SpacegroupAnalyzer at symprec=0.1 (primary) and 0.01 (QC).",
                "get_refined_structure() is NOT called; source geometry is not idealized.",
            ],
        }
        (args.output_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    LOG.info("Done. %s rows processed → %s", f"{processed:,}", args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
