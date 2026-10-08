import json
from pathlib import Path
from typing import Any

from aflow.normalize_reported_values import (
    _build_sg_reference,
    fuzzy_map,
    load_config,
    normalize_row,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_aflow(input_path: Path) -> list[dict[str, Any]]:
    with input_path.open("r", encoding="utf-8") as f:
        json_data = json.load(f)

    if isinstance(json_data, dict):
        raw_records = [json_data] if "auid" in json_data else list(json_data.values())
    elif isinstance(json_data, list):
        raw_records = json_data
    else:
        raise TypeError("Unsupported AFLOW JSON format")

    records: list[dict[str, Any]] = [r for r in raw_records if isinstance(r, dict)]

    if not records:
        raise ValueError("No material records found")

    return records


def normalize_aflow(records: list[dict]) -> list[dict]:
    config = PROJECT_ROOT / "field_map_aflow.yaml"

    mapping = load_config(config, "aflow")
    headers = list({key for record in records for key in record})
    mapping = fuzzy_map(headers, mapping)

    _build_sg_reference()

    results = []

    for index, record in enumerate(records, start=1):
        normalized, issues = normalize_row(
            row=record,
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


def main(input_path: Path, output_path: Path):
    print("Hello from aflow db")

    records = load_aflow(input_path)
    results = normalize_aflow(records)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"Loaded {len(records)} AFLOW records")
    print(f"Normalized {len(results)} records")
    print(f"Saved to: {output_path}")
    print(json.dumps(results[0]["normalized"], indent=2))
