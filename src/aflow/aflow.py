import json
from pathlib import Path
from typing import Any

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
            enable_geometry=False,
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
                "issues": issues,
            }
        )

    return results


def main(
    input_path: Path,
    output_path: Path,
    config_path: Path,
) -> None:

    records = load_aflow(input_path)
    results = normalize_aflow(records, config_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"Loaded {len(records)} AFLOW records")
    print(f"Normalized {len(results)} records")
    print(f"Saved to: {output_path}")

    print(json.dumps(results[0]["normalized"], indent=2))
