import json
from pathlib import Path
from typing import Any

from aflow import constants
from aflow.normalize_reported_values import (
    _build_sg_reference,
    is_null,
    load_config,
    normalize_row,
)


def prepare_aflow_record(record: dict) -> dict:
    prepared = record.copy()

    # Extract lattice parameters
    geometry = record.get("geometry")

    if isinstance(geometry, list) and len(geometry) == 6:
        fields = [
            "lattice_a",
            "lattice_b",
            "lattice_c",
            "lattice_alpha",
            "lattice_beta",
            "lattice_gamma",
        ]
        prepared.update(zip(fields, geometry))

    # Extract Wyckoff positions
    letters = record.get("Wyckoff_letters")
    mults = record.get("Wyckoff_multiplicities")

    if (
        isinstance(letters, list)
        and isinstance(mults, list)
        and len(letters) == len(mults)
        and all(
            isinstance(lg, list) and isinstance(mg, list) and len(lg) == len(mg)
            for lg, mg in zip(letters, mults)
        )
    ):
        positions = [
            f"{mult}{letter}"
            for lg, mg in zip(letters, mults)
            for letter, mult in zip(lg, mg)
        ]
        if positions:
            prepared["wyckoff"] = " ".join(positions)

    return prepared


def load_aflow(input_path: Path) -> list[dict[str, Any]]:
    with input_path.open("r", encoding="utf-8") as f:
        json_data = json.load(f)

    if isinstance(json_data, dict):
        raw_records = [json_data] if "auid" in json_data else list(json_data.values())
    elif isinstance(json_data, list):
        raw_records = json_data
    else:
        raise TypeError("Unsupported AFLOW JSON format")

    records = [record for record in raw_records if isinstance(record, dict)]

    if not records:
        raise ValueError("No material records found")

    return records


def normalize_aflow(
    records: list[dict[str, Any]],
    config_path: Path,
) -> list[dict[str, Any]]:

    mapping = load_config(config_path, "aflow")

    _build_sg_reference()

    results = []

    for index, record in enumerate(records, start=1):
        prepared = prepare_aflow_record(record)

        normalized, issues = normalize_row(
            row=prepared,
            mapping=mapping,
            row_id=index,
            db_name="aflow",
            sites_index={},
            enable_geometry=True,
        )

        results.append(
            {
                "auid": record.get("auid"),
                "raw": record,
                "normalized": {
                    key: value
                    for key, value in normalized.items()
                    if key.startswith("normalized_") and value != ""
                },
                "derived": {
                    key: value
                    for key, value in normalized.items()
                    if key.startswith(("derived_", "qc_")) and value != ""
                },
                "consistency": {
                    key: normalized[key]
                    for key in constants.CONSISTENCY_FIELDS
                    if normalized.get(key) != ""
                },
                "quality_status": normalized["quality_status"],
                "issues": issues,
            }
        )

    return results


def merge_aflow_record(
    record: dict[str, Any], mapping: dict[str, str]
) -> dict[str, Any]:
    """Preserve reported values and make computed values and quality traceable."""
    raw = record["raw"]
    normalized = record.get("normalized", {})
    derived = record.get("derived", {})
    sources = {}

    def select_value(field: str, convert=None, raw_key: str | None = None):
        key = raw_key or mapping.get(field, field)
        derived_key = (
            "derived_reduced_formula"
            if field == "formula_reduced"
            else f"derived_{field}"
        )
        for origin, value in (
            ("normalized", normalized.get(f"normalized_{field}")),
            ("reported", raw.get(key)),
            ("derived", derived.get(derived_key)),
        ):
            if value not in (None, "", "unknown"):
                sources[key] = origin
                return convert(value) if convert and origin != "reported" else value
        sources[key] = "missing"
        return None

    geometry = [
        select_value(f"lattice_{name}", float) for name in constants.LATTICE_PARAMETERS
    ]
    if None in geometry:
        geometry = raw.get(mapping["geometry"])
        sources[mapping["geometry"]] = "reported" if geometry is not None else "missing"
    else:
        origins = {sources[f"lattice_{name}"] for name in constants.LATTICE_PARAMETERS}
        sources[mapping["geometry"]] = origins.pop() if len(origins) == 1 else "mixed"

    merged = raw.copy()
    merged.update(
        {
            # Chemistry
            mapping["formula"]: select_value(
                "formula_reduced", raw_key=mapping["formula"]
            ),
            "formula_hill": select_value("formula_hill"),
            "formula_anonymous": select_value("formula_anonymous"),
            "chemical_system": select_value("chemical_system"),
            "nelements": select_value("nelements", int),
            # Symmetry
            mapping["spacegroup"]: select_value(
                "spacegroup_ita", int, raw_key=mapping["spacegroup"]
            ),
            mapping["pearson_symbol"]: select_value("pearson_symbol"),
            mapping["crystal_system"]: select_value("crystal_system"),
            mapping["bravais_lattice"]: select_value("bravais_lattice"),
            mapping["point_group"]: select_value(
                "point_group_hm", raw_key=mapping["point_group"]
            ),
            mapping["point_group_schoenflies"]: select_value("point_group_schoenflies"),
            "spacegroup_hm_short": select_value("spacegroup_hm_short"),
            "spacegroup_hm_full": select_value("spacegroup_hm_full"),
            "spacegroup_hall": select_value("spacegroup_hall"),
            "spacegroup_schoenflies": select_value("spacegroup_schoenflies"),
            "pearson_atom_count": select_value("pearson_atom_count", int),
            "primitive_spacegroup_ita": select_value("primitive_spacegroup_ita", int),
            "primitive_hm_short": select_value("primitive_hm_short"),
            "conventional_spacegroup_ita": select_value(
                "conventional_spacegroup_ita", int
            ),
            "conventional_hm_short": select_value("conventional_hm_short"),
            # Geometry and dimensionality
            mapping["geometry"]: geometry,
            mapping["lattice_volume"]: select_value("lattice_volume", float),
            mapping["nsites"]: select_value("nsites", int),
            mapping["site_frac_coords"]: select_value("site_frac_coords"),
            "primitive_nsites": select_value("primitive_nsites", int),
            "conventional_nsites": select_value("conventional_nsites", int),
            "lattice_matrix": select_value("lattice_matrix"),
            "periodic_dimension": select_value("periodic_dimension", int),
            "dimensionality": select_value("dimensionality", int),
            "layer_group": select_value("layer_group_ita", int, raw_key="layer_group"),
        }
    )

    units = {
        mapping["geometry"]: ["angstrom"] * 3 + ["degree"] * 3,
        mapping["lattice_volume"]: "angstrom^3",
        mapping["site_frac_coords"]: "dimensionless",
        "lattice_matrix": "angstrom",
    }
    for field, unit in constants.AFLOW_SCALAR_UNITS.items():
        key = f"normalized_{field}"
        if key in normalized:
            merged[field] = normalized[key]
            units[field] = unit
            if normalized[key] is not None:
                sources[field] = "normalized"
            elif is_null(raw.get(field)):
                sources[field] = "missing"
            else:
                sources[field] = "invalid_reported"
    if "normalized_stoichiometry" in normalized:
        merged["stoichiometry"] = normalized["normalized_stoichiometry"]
        if merged["stoichiometry"] is not None:
            sources["stoichiometry"] = "normalized"
        elif is_null(raw.get("stoichiometry")):
            sources["stoichiometry"] = "missing"
        else:
            sources["stoichiometry"] = "invalid_reported"
        units["stoichiometry"] = "dimensionless"

    merged["quality"] = {
        "status": record["quality_status"],
        "checks": record.get("consistency", {}),
        "issues": record.get("issues", []),
        "geometry_status": normalized.get("normalized_geometry_status"),
        "pbc_status": derived.get("derived_pbc_status"),
        "layer_group_status": derived.get("derived_layer_group_status"),
        "dimensionality_warnings": json.loads(
            derived.get("derived_dimensionality_warnings", "[]")
        ),
    }
    merged["provenance"] = {
        "field_sources": {
            field: origin for field, origin in sources.items() if field in merged
        },
        "retained_fields": "reported",
        "geometry_stage": "relaxed",
        "energy_cell_basis": "reported_simulation_cell",
        "symprec_angstrom": derived.get("derived_symprec"),
        "qc_symprec_angstrom": derived.get("derived_qc_symprec"),
        "angle_tolerance_degrees": derived.get("derived_angle_tolerance"),
        "dimensionality_method": derived.get("derived_dimensionality_method"),
        "dimensionality_parameters": json.loads(
            derived.get("derived_dimensionality_parameters", "{}")
        ),
    }
    merged["units"] = units
    return merged


def main(
    input_path: Path,
    output_path: Path,
    config_path: Path,
) -> None:

    records = load_aflow(input_path)
    results = normalize_aflow(records, config_path)
    mapping = load_config(config_path, "aflow")
    merged_results = [merge_aflow_record(record, mapping) for record in results]
    intermediary_path = output_path.with_name(
        f"{output_path.stem.removesuffix('_normalized')}_intermediary{output_path.suffix}"
    )
    for record in merged_results:
        record["provenance"]["intermediary_file"] = intermediary_path.name

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with intermediary_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(merged_results, f, indent=2, ensure_ascii=False)

    print(f"Loaded {len(records)} AFLOW records")
    print(f"Normalized {len(results)} records")
    print(f"Intermediary saved to: {intermediary_path}")
    print(f"Saved to: {output_path}")

    print(json.dumps(merged_results[0], indent=2))
