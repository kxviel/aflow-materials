import json
from pathlib import Path
from typing import Any

from aflow import constants
from aflow.normalize_reported_values import (
    _build_sg_reference,
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
                    for key in (
                        "formula_consistency",
                        "spacegroup_reported_vs_derived",
                        "crystal_system_reported_vs_derived",
                        "symmetry_tolerance_sensitive",
                        "lattice_volume_consistency",
                        "pearson_atom_count_consistency",
                    )
                    if normalized.get(key) != ""
                },
                "issues": issues,
            }
        )

    return results


def merge_aflow_record(
    record: dict[str, Any], mapping: dict[str, str]
) -> dict[str, Any]:
    """Keep AFLOW's arrays and select normalized, derived, then raw values."""
    raw = record["raw"]
    normalized = record.get("normalized", {})
    derived = record.get("derived", {})

    def select_value(field: str, convert=None, raw_key: str | None = None):
        for value in (
            normalized.get(f"normalized_{field}"),
            derived.get(f"derived_{field}"),
        ):
            if value not in (None, "", "unknown"):
                return convert(value) if convert else value
        return raw.get(raw_key or mapping.get(field, field))

    geometry = [
        select_value(f"lattice_{name}", float)
        for name in constants.LATTICE_PARAMETERS
    ]
    if None in geometry:
        geometry = raw.get(mapping["geometry"])

    merged = raw.copy()
    merged.update(
        {
            # Chemistry
            mapping["formula"]: (
                normalized.get("normalized_formula_reduced")
                or derived.get("derived_reduced_formula")
                or raw.get(mapping["formula"])
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
            mapping["point_group_schoenflies"]: select_value(
                "point_group_schoenflies"
            ),
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
