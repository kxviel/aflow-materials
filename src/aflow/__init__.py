"""Material DB using AFlow"""

from __future__ import annotations

from pathlib import Path

from .aflow import main as aflow

__all__ = ["main"]

PROJECT_ROOT = Path.cwd().parent


def main() -> None:

    # if len(sys.argv) != 2 or sys.argv[1].lower() not in ("agp", "ggmp"):
    #     print("Invalid arguments!")
    #     print("Usage: uv run gai-cal ggmp | uv run gai-cal agp")
    #     return

    # DATASET = Literal["AGP", "GGMP"]
    # dataset: DATASET = "AGP" if sys.argv[1].lower() == "agp" else "GGMP"

    # meta_path = PROJECT_ROOT / "datasets/processed" / dataset / "meta.tsv"
    # otu_path = PROJECT_ROOT / "datasets/processed" / dataset / "otu.tsv"
    # output_path = PROJECT_ROOT / "result" / dataset

    aflow()
