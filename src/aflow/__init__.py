"""Material DB using AFlow"""

from __future__ import annotations

from pathlib import Path

from .aflow import main as aflow

__all__ = ["main"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"


def main() -> None:

    input_path: Path = DATA_DIR / "aflow_full_200.json"
    output_path: Path = DATA_DIR / "aflow_full_200_normalized.json"
    config_path: Path = PROJECT_ROOT / "field_map.yaml"

    aflow(input_path, output_path, config_path)
