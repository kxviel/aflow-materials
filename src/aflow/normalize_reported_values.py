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
import sys
import time
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Any

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aflow import constants, derive

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

            if info is None:
                continue

            ita = int(info.number)
            if not (1 <= ita <= 230):
                continue

            hm_short = info.international_short.strip()
            hm_full = info.international_full.strip()
            hall_sym = info.hall_symbol.strip()
            schoen = info.schoenflies.strip()
            choice = info.choice.strip()

            # Store first occurrence per ITA (primary setting)
            if ita not in seen_ita:
                seen_ita[ita] = {
                    "hm_short": hm_short,
                    "hm_full": hm_full,
                    "hall_symbol": hall_sym,
                    "schoenflies": schoen,
                    "choice": choice,
                    "crystal_system": constants.SPACEGROUP_CRYSTAL_SYSTEM.get(ita, ""),
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


NULLS = {"", "na", "n/a", "nan", "none", "null", "_", "-", "--", "?", "unknown"}


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
    "pearson_symbol": ["pearson_symbol", "pearson"],
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


def get_array_field(row: dict, mapping: dict[str, str], canon: str) -> list | None:
    """Accept JSON arrays from CSV and native arrays from JSON records."""
    value = row.get(mapping.get(canon, ""))
    if is_null(value):
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError(f"{canon} must be an array")
    return value


# ══════════════════════════════════════════════════════════════════════════════
# SPACE GROUP AUTO-DETECTION & NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════


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
    for alias in constants.CRYSTAL_SYSTEM_MAP:
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
        cs = constants.CRYSTAL_SYSTEM_MAP.get(low.strip(), "")
        if not cs:
            for alias, canon in constants.CRYSTAL_SYSTEM_MAP.items():
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
    if low in constants.CRYSTAL_SYSTEM_MAP:
        return constants.CRYSTAL_SYSTEM_MAP[low]
    for alias, canon in constants.CRYSTAL_SYSTEM_MAP.items():
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
    if s in constants.BRAVAIS_CANONICAL:
        return s
    # Alias lookup (case-insensitive)
    key = s.lower()
    if key in constants.BRAVAIS_ALIASES:
        return constants.BRAVAIS_ALIASES[key]
    # Fuzzy: try to find a substring alias
    for alias, canon in constants.BRAVAIS_ALIASES.items():
        if alias in key:
            return canon
    return ""


def normalize_pearson(raw: str) -> tuple[str, int | None]:
    match = re.fullmatch(
        r"([amothc])\s*([PCIFRABS])\s*([1-9]\d*)", raw.strip(), re.IGNORECASE
    )
    if match:
        prefix = match[1].lower() + match[2].upper()
        if normalize_bravais(prefix):
            count = int(match[3])
            return f"{prefix}{count}", count
    return "", None


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
    if key in constants.POINT_GROUP_TABLE:
        return constants.POINT_GROUP_TABLE[key]

    # Try HM → Schoenflies lookup
    hm_key = s_clean.replace(" ", "").replace("−", "-")
    if hm_key in constants.HM_TO_SCHOENFLIES:
        schoen = constants.HM_TO_SCHOENFLIES[hm_key]
        return constants.POINT_GROUP_TABLE.get(schoen.lower(), (schoen, hm_key))

    # Fallback: try partial match
    for k, v in constants.POINT_GROUP_TABLE.items():
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


def normalize_dimensionality(raw: str, layer_count_raw: str = "") -> tuple[str, str]:
    """Returns (dimensionality: '0'/'1'/'2'/'3'/'unknown', material_form)."""
    if is_null(raw):
        dim = "unknown"
    else:
        low = raw.strip().lower().replace("_", " ")
        number = safe_int(raw)
        dim = str(number) if number in (0, 1, 2, 3) else "unknown"
        if dim == "unknown":
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
    spacegroup_ita: int,
    wyckoff_entries: list[dict],
    reported_site_symmetries: list[str] | None = None,
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

    if reported_site_symmetries is not None:
        labels = [entry["multiplicity"] + entry["letter"] for entry in wyckoff_entries]
        mults = [entry["multiplicity"] for entry in wyckoff_entries]
        return labels, mults, reported_site_symmetries, "ok"

    try:
        from pymatgen.symmetry.groups import SpaceGroup

        sg = SpaceGroup.from_int_number(spacegroup_ita)
        wyk_sites = {ws.letter: ws for ws in getattr(sg, "wyckoff_sites", [])}

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


def parse_lattice_parameters(row: dict, mapping: dict[str, str]) -> list[float] | None:
    """Return six positive parameters, or None for missing or invalid values."""
    parameters: list[float] = []
    for name in constants.LATTICE_PARAMETERS:
        value = safe_float(get_field(row, mapping, f"lattice_{name}"))
        if value is None or value <= 0:
            return None
        parameters.append(value)
    return parameters


def build_geometry_payload(
    row: dict, mapping: dict[str, str], sites_index: dict[str, list[dict]]
) -> dict:
    """Assemble lattice + sites data for derive_from_geometry()."""
    payload: dict[str, Any] = {}

    # Lattice matrix from raw_lattice_matrix field
    matrix = get_array_field(row, mapping, "lattice_matrix")
    if matrix is not None:
        if len(matrix) != 3 or any(
            not isinstance(r, list) or len(r) != 3 for r in matrix
        ):
            raise ValueError("lattice_matrix must have shape 3 x 3")
        payload["lattice_matrix"] = [[float(x) for x in r] for r in matrix]

    # Fallback: build matrix from a,b,c,alpha,beta,gamma
    if "lattice_matrix" not in payload:
        try:
            from pymatgen.core import Lattice

            parameters = parse_lattice_parameters(row, mapping)
            if parameters is not None:
                lattice = Lattice.from_parameters(*parameters)
                payload["lattice_matrix"] = lattice.matrix.tolist()
                payload["lattice_matrix_method"] = "six_lattice_parameters"
        except Exception:
            pass

    # PBC
    pbc = get_array_field(row, mapping, "pbc")
    if pbc is not None and (len(pbc) != 3 or any(type(x) is not bool for x in pbc)):
        raise ValueError("pbc must contain three booleans")
    payload["pbc"] = pbc if pbc is not None else [True, True, True]
    payload["pbc_reported"] = pbc is not None

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
        species = get_array_field(row, mapping, "site_species")
        if species is None:
            elements = get_array_field(row, mapping, "element_species")
            counts = (
                get_array_field(row, mapping, "composition")
                if elements is not None
                else None
            )
            if elements is not None and counts is not None:
                if len(elements) != len(counts):
                    raise ValueError(
                        "Element species and composition counts must align"
                    )
                species = []
                for element, raw_count in zip(elements, counts):
                    count = safe_int(raw_count)
                    if count is None or count <= 0:
                        raise ValueError("Composition counts must be positive integers")
                    species.extend([element] * count)
        if species is not None:
            payload["site_species"] = species

    if "site_frac_coords" not in payload:
        coords = get_array_field(row, mapping, "site_frac_coords")
        if coords is not None:
            if any(not isinstance(r, list) or len(r) != 3 for r in coords):
                raise ValueError("Fractional coordinates must have shape N x 3")
            payload["site_frac_coords"] = [[float(x) for x in r] for r in coords]

    if "lattice_matrix" in payload:
        from pymatgen.core import Lattice

        matrix = payload["lattice_matrix"]
        if (
            not all(math.isfinite(x) for r in matrix for x in r)
            or Lattice(matrix).volume <= 1e-8
        ):
            raise ValueError("Lattice matrix must be finite and nonsingular")
    if "site_frac_coords" in payload:
        coords = payload["site_frac_coords"]
        if not coords or not all(math.isfinite(x) for r in coords for x in r):
            raise ValueError("Fractional coordinates must be nonempty and finite")
        if "site_species" in payload and len(payload["site_species"]) != len(coords):
            raise ValueError(
                "Site species and fractional coordinates have different lengths"
            )
        raw_nsites = get_field(row, mapping, "nsites")
        if not is_null(raw_nsites) and safe_int(raw_nsites) != len(coords):
            raise ValueError("Reported site count differs from the coordinate count")

    return payload


def normalize_geometry(
    row: dict,
    mapping: dict[str, str],
    sites_index: dict[str, list[dict]],
    output: dict[str, Any],
) -> dict:
    """Add reported geometry values to output and return geometry for derivation."""
    reported_geometry = get_array_field(row, mapping, "geometry")
    if reported_geometry is not None and len(reported_geometry) != 6:
        raise ValueError("Geometry must contain six lattice parameters")

    for name in (*constants.LATTICE_PARAMETERS, "volume"):
        raw = output[f"raw_lattice_{name}"]
        value = safe_float(raw)
        if not is_null(raw) and (
            value is None
            or value <= 0
            or (name in ("alpha", "beta", "gamma") and value >= 180)
        ):
            raise ValueError(f"Invalid lattice {name}: {raw}")
        output[f"normalized_lattice_{name}"] = value

    raw_nsites = output["raw_nsites"]
    nsites = safe_int(raw_nsites)
    if not is_null(raw_nsites) and (nsites is None or nsites <= 0):
        raise ValueError("Site count must be a positive integer")
    output["normalized_nsites"] = nsites

    geometry = build_geometry_payload(row, mapping, sites_index)
    for name in ("site_frac_coords", "site_species"):
        # Only inline reported arrays belong in normalized geometry.
        if mapping.get(name) or (
            name == "site_species" and mapping.get("element_species")
        ):
            output[f"normalized_{name}"] = geometry.get(name)
    if mapping.get("lattice_matrix"):
        output["normalized_lattice_matrix"] = (
            geometry.get("lattice_matrix")
            if get_field(row, mapping, "lattice_matrix")
            else None
        )
    if geometry.get("pbc_reported"):
        output["normalized_pbc"] = geometry["pbc"]

    complete = all(
        geometry.get(name)
        for name in ("lattice_matrix", "site_species", "site_frac_coords")
    )
    output["normalized_geometry_status"] = "valid" if complete else "incomplete"
    return geometry


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

    out: dict[str, Any] = {f: "" for f in constants.CANONICAL_FIELDS}
    out["source_row_id"] = str(row_id)
    out["source_database"] = db_name
    out["source_material_id"] = mat_id
    out["normalization_policy_version"] = POLICY_VERSION

    # ── RAW VALUES ─────────────────────────────────────────────────────────────
    for canon_suffix in [
        "spacegroup",
        "crystal_system",
        "bravais_lattice",
        "pearson_symbol",
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
        "site_frac_coords",
    ]:
        out[f"raw_{canon_suffix}"] = get_field(row, mapping, canon_suffix)

    # Standardize reported geometry without replacing it with computed values.
    try:
        geo = normalize_geometry(row, mapping, sites_index, out)
    except (ValueError, TypeError, OverflowError) as exc:
        geo = {}
        out["normalized_geometry_status"] = "invalid"
        issue("geometry", "GEOMETRY_INVALID", "", str(exc), "error")

    # ── DERIVED (GEOMETRY) ─────────────────────────────────────────────────────
    derived: dict[str, Any] = {}
    if enable_geometry:
        derived = derive.derive_from_geometry(geo, _SG_REF)
        if derived["derived_status"] == "failed":
            issue(
                "geometry",
                "GEOMETRY_DERIVATION_FAILED",
                "",
                derived["derived_error"],
                "error",
            )
        elif (
            derived["derived_status"] == "skipped_missing_geometry"
            and out["normalized_geometry_status"] != "invalid"
        ):
            issue(
                "geometry",
                "GEOMETRY_MISSING",
                "",
                "Lattice, site species or fractional coordinates are missing",
                "info",
            )
        if derived.get("derived_dimensionality_error"):
            issue(
                "dimensionality",
                "DIMENSIONALITY_DERIVATION_FAILED",
                "",
                derived["derived_dimensionality_error"],
            )
        if derived.get("derived_layer_group_error"):
            issue(
                "layer_group",
                "LAYER_GROUP_DERIVATION_FAILED",
                "",
                derived["derived_layer_group_error"],
            )
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
    raw_pearson = out["raw_pearson_symbol"]
    norm_bv = normalize_bravais(raw_pearson) or normalize_bravais(raw_bv)
    out["normalized_bravais_lattice"] = norm_bv
    pearson, pearson_count = normalize_pearson(raw_pearson)
    out["normalized_pearson_symbol"] = pearson or None
    out["normalized_pearson_atom_count"] = pearson_count
    if not is_null(raw_pearson) and not pearson:
        issue(
            "pearson_symbol",
            "PEARSON_PARSE_FAILED",
            raw_pearson,
            "Invalid Pearson family, centering or atom count",
        )
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
    out["normalized_dimensionality"] = dim if dim != "unknown" else None
    if is_null(raw_dim):
        dimensionality_status = "not_reported"
    elif dim == "unknown":
        dimensionality_status = "unrecognized"
    else:
        dimensionality_status = "ok"
    out["normalized_dimensionality_status"] = dimensionality_status
    out["normalized_material_form"] = form

    # ── NORMALIZED: LAYER GROUP ────────────────────────────────────────────────
    raw_lg = out["raw_layer_group"]
    lg_ita, lg_status = normalize_layer_group(raw_lg)
    out["normalized_layer_group_ita"] = lg_ita or None
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
        reported_syms = row.get(mapping.get("wyckoff_site_symmetries", ""))
        reported_letters = row.get(mapping.get("wyckoff_letters", ""))
        site_syms = None
        if isinstance(reported_syms, list) and isinstance(reported_letters, list):
            aligned = len(reported_syms) == len(reported_letters) and all(
                isinstance(syms, list)
                and isinstance(letters, list)
                and len(syms) == len(letters)
                for syms, letters in zip(reported_syms, reported_letters)
            )
            if aligned and sum(map(len, reported_syms)) == len(parsed_wyk):
                site_syms = [txt(sym) for group in reported_syms for sym in group]
            else:
                issue(
                    "wyckoff",
                    "WYCKOFF_LENGTH_MISMATCH",
                    raw_wyk,
                    "Reported Wyckoff letters and site symmetries have different lengths",
                )
        wlabels, wmults, wsyms, wstatus = lookup_wyckoff(wyk_ita, parsed_wyk, site_syms)
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
        from pymatgen.core import Composition

        out["formula_consistency"] = (
            "consistent"
            if Composition(d_formula).reduced_composition
            == Composition(n_formula).reduced_composition
            else "mismatch"
        )
        if out["formula_consistency"] == "mismatch":
            issue(
                "formula",
                "REPORTED_DERIVED_FORMULA_MISMATCH",
                raw_formula,
                "Reported formula and geometry have different reduced compositions",
            )
    elif n_formula and not d_formula:
        out["formula_consistency"] = "reported_only"
    elif d_formula and not n_formula:
        out["formula_consistency"] = "derived_only"
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
    if out["symmetry_tolerance_sensitive"] == "yes":
        issue(
            "spacegroup",
            "SYMMETRY_TOLERANCE_SENSITIVE",
            raw_sg,
            f"Derived space group differs between symprec={constants.PRIMARY_SYMPREC} and {constants.QC_SYMPREC} Angstrom",
        )

    volume = out.get("normalized_lattice_volume")
    derived_volume = safe_float(out.get("derived_lattice_volume"))
    if volume is not None and derived_volume is not None:
        matches = math.isclose(volume, derived_volume, rel_tol=1e-4, abs_tol=1e-3)
        out["lattice_volume_consistency"] = "consistent" if matches else "mismatch"
        if not matches:
            issue(
                "lattice_volume",
                "LATTICE_VOLUME_MISMATCH",
                out["raw_lattice_volume"],
                f"Reported volume={volume}, lattice-derived volume={derived_volume}",
            )

    cell_count = out.get("derived_pearson_atom_count")
    if pearson_count is not None and cell_count:
        matches = pearson_count == cell_count
        out["pearson_atom_count_consistency"] = "consistent" if matches else "mismatch"
        if not matches:
            issue(
                "pearson_symbol",
                "PEARSON_ATOM_COUNT_MISMATCH",
                raw_pearson,
                f"Pearson count={pearson_count}, derived {out['derived_pearson_cell_setting']}-cell count={cell_count}",
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
        derive._imports()  # warm up pymatgen import
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
            out_row = {f: "" for f in constants.CANONICAL_FIELDS}
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
            out_dir / "normalized_output.csv", constants.CANONICAL_FIELDS
        )
        self.handles.append(fh)
        fh, self.issues = open_output_csv(
            out_dir / "normalization_issues.csv", constants.ISSUE_FIELDS
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
            "primary_symprec_angstrom": constants.PRIMARY_SYMPREC,
            "qc_symprec_angstrom": constants.QC_SYMPREC,
            "angle_tolerance_degrees": constants.ANGLE_TOLERANCE,
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
