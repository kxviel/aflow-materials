"""Fixed lookup tables, output fields, and shared symmetry tolerances."""

from __future__ import annotations

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
SPACEGROUP_CRYSTAL_SYSTEM: dict[int, str] = {}
for _sg in range(1, 3):
    SPACEGROUP_CRYSTAL_SYSTEM[_sg] = "triclinic"
for _sg in range(3, 16):
    SPACEGROUP_CRYSTAL_SYSTEM[_sg] = "monoclinic"
for _sg in range(16, 75):
    SPACEGROUP_CRYSTAL_SYSTEM[_sg] = "orthorhombic"
for _sg in range(75, 143):
    SPACEGROUP_CRYSTAL_SYSTEM[_sg] = "tetragonal"
for _sg in range(143, 168):
    SPACEGROUP_CRYSTAL_SYSTEM[_sg] = "trigonal"
for _sg in range(168, 195):
    SPACEGROUP_CRYSTAL_SYSTEM[_sg] = "hexagonal"
for _sg in range(195, 231):
    SPACEGROUP_CRYSTAL_SYSTEM[_sg] = "cubic"

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
BRAVAIS_ALIASES: dict[str, str] = {}
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
        BRAVAIS_ALIASES[_a.lower()] = _canon

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
HM_TO_SCHOENFLIES: dict[str, str] = {v[1]: v[0] for v in POINT_GROUP_TABLE.values()}

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
    "raw_pearson_symbol",
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
    "raw_site_frac_coords",
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
    "derived_pearson_symbol",
    "derived_pearson_atom_count",
    "derived_pearson_cell_setting",
    "derived_primitive_nsites",
    "derived_conventional_nsites",
    "derived_lattice_matrix",
    "derived_lattice_matrix_method",
    "derived_symprec",
    "derived_qc_symprec",
    "derived_angle_tolerance",
    "derived_periodic_dimension",
    "derived_pbc_status",
    "derived_dimensionality",
    "derived_dimensionality_status",
    "derived_dimensionality_method",
    "derived_dimensionality_parameters",
    "derived_dimensionality_warnings",
    "derived_dimensionality_error",
    "derived_layer_group_ita",
    "derived_layer_group_status",
    "derived_layer_group_error",
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
    "normalized_pearson_symbol",
    "normalized_pearson_atom_count",
    "normalized_lattice_a",
    "normalized_lattice_b",
    "normalized_lattice_c",
    "normalized_lattice_alpha",
    "normalized_lattice_beta",
    "normalized_lattice_gamma",
    "normalized_lattice_volume",
    "normalized_lattice_matrix",
    "normalized_site_species",
    "normalized_site_frac_coords",
    "normalized_nsites",
    "normalized_pbc",
    "normalized_geometry_status",
    "normalized_point_group_schoenflies",
    "normalized_point_group_hm",
    "normalized_formula_hill",
    "normalized_formula_reduced",
    "normalized_formula_anonymous",
    "normalized_composition_json",
    "normalized_chemical_system",
    "normalized_nelements",
    "normalized_dimensionality",
    "normalized_dimensionality_status",
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
    "lattice_volume_consistency",
    "pearson_atom_count_consistency",
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

LATTICE_PARAMETERS = ("a", "b", "c", "alpha", "beta", "gamma")

PRIMARY_SYMPREC = 0.1
QC_SYMPREC = 0.01
ANGLE_TOLERANCE = 5.0
