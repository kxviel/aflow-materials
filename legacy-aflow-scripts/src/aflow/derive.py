"""Calculate material properties from prepared crystal geometry."""

from __future__ import annotations

import json
import logging
import warnings
from collections import Counter
from typing import Any

from aflow import constants

LOG = logging.getLogger("normalizer")


def _compact_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _imports():
    try:
        from pymatgen.core import Lattice, Structure
        from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

        return Lattice, Structure, SpacegroupAnalyzer
    except ImportError as exc:
        raise RuntimeError("pymatgen/numpy not available") from exc


def _standard_cell_fields(
    structure, cell_kind: str, spacegroup_reference: dict[int, dict]
) -> dict[str, Any]:
    """Return symmetry and site count for a primitive or conventional cell."""
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

    analyzer = SpacegroupAnalyzer(
        structure,
        symprec=constants.PRIMARY_SYMPREC,
        angle_tolerance=constants.ANGLE_TOLERANCE,
    )
    dataset = analyzer.get_symmetry_dataset()
    fields: dict[str, Any] = {}
    if dataset is not None:
        number = int(dataset.number if hasattr(dataset, "number") else dataset["number"])
        fields[f"derived_{cell_kind}_spacegroup_ita"] = str(number)
        fields[f"derived_{cell_kind}_hm_short"] = spacegroup_reference.get(
            number, {}
        ).get("hm_short", "")
    fields[f"derived_{cell_kind}_nsites"] = len(structure)
    return fields


def derive_from_geometry(row_data: dict, spacegroup_reference: dict[int, dict]) -> dict:
    """
    Build a pymatgen Structure from lattice + sites in row_data, then derive
    symmetry, composition, Wyckoff positions.
    row_data keys: lattice_matrix, pbc, site_species (list of species dicts),
                   site_frac_coords (list of [x,y,z]).
    Returns dict of derived_ values.
    """
    out: dict[str, Any] = {
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
        Lattice, Structure, SpacegroupAnalyzer = _imports()

        pbc = row_data.get("pbc", [True, True, True])
        lattice = Lattice(matrix, pbc=tuple(pbc))
        structure = Structure(
            lattice,
            species,
            frac_coords,
            coords_are_cartesian=False,
            to_unit_cell=False,
            validate_proximity=True,
        )

        # Composition
        composition = structure.composition
        out["derived_formula"] = composition.formula.replace(" ", "")
        out["derived_reduced_formula"] = composition.reduced_formula
        out["derived_formula_hill"] = composition.hill_formula.replace(" ", "")
        out["derived_formula_anonymous"] = composition.anonymized_formula
        out["derived_composition_json"] = _compact_json(
            {element.symbol: float(count) for element, count in composition.items()}
        )
        try:
            out["derived_chemical_system"] = composition.chemical_system
        except Exception:
            out["derived_chemical_system"] = "-".join(
                sorted(element.symbol for element in composition.elements)
            )
        out["derived_nelements"] = str(len(composition.elements))
        out["derived_nsites"] = str(len(structure))

        # Lattice params
        lattice = structure.lattice
        out["derived_lattice_a"] = str(round(lattice.a, 8))
        out["derived_lattice_b"] = str(round(lattice.b, 8))
        out["derived_lattice_c"] = str(round(lattice.c, 8))
        out["derived_lattice_alpha"] = str(round(lattice.alpha, 6))
        out["derived_lattice_beta"] = str(round(lattice.beta, 6))
        out["derived_lattice_gamma"] = str(round(lattice.gamma, 6))
        out["derived_lattice_volume"] = str(round(lattice.volume, 6))
        out["derived_lattice_matrix"] = lattice.matrix.tolist()
        out["derived_lattice_matrix_method"] = row_data.get(
            "lattice_matrix_method", "reported_matrix"
        )
        out["derived_symprec"] = constants.PRIMARY_SYMPREC
        out["derived_qc_symprec"] = constants.QC_SYMPREC
        out["derived_angle_tolerance"] = constants.ANGLE_TOLERANCE
        out["derived_periodic_dimension"] = sum(pbc)
        out["derived_pbc_status"] = (
            "reported" if row_data.get("pbc_reported") else "assumed_full_periodicity"
        )
        out.update(derive_dimensionality(structure))
        if not all(pbc):
            out["derived_status"] = "ok"
            return out  # Three-periodic space-group analysis does not apply.

        # Primary symmetry (0.1 Å)
        sga = SpacegroupAnalyzer(
            structure,
            symprec=constants.PRIMARY_SYMPREC,
            angle_tolerance=constants.ANGLE_TOLERANCE,
        )
        sd = sga.get_symmetry_dataset()
        if sd is not None:
            ita = int(sd.number if hasattr(sd, "number") else sd["number"])
            out["derived_spacegroup_ita"] = str(ita)
            ref = spacegroup_reference.get(ita, {})
            out["derived_spacegroup_hm_short"] = ref.get("hm_short", "")
            out["derived_spacegroup_hm_full"] = ref.get("hm_full", "")
            out["derived_spacegroup_hall"] = ref.get("hall_symbol", "")
            out["derived_spacegroup_schoenflies"] = ref.get("schoenflies", "")
            out["derived_crystal_system"] = ref.get("crystal_system", "")
            pg_hm = sga.get_point_group_symbol()
            pg_s = constants.HM_TO_SCHOENFLIES.get(pg_hm, "")
            out["derived_point_group_schoenflies"] = pg_s
            out["derived_point_group_hm"] = pg_hm

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
            if centering in ("A", "B", "C") and cs_abbr in ("m", "o"):
                centering = "S"
            prefix = cs_abbr + centering
            out["derived_bravais_lattice"] = (
                prefix if prefix in constants.BRAVAIS_CANONICAL else ""
            )

        # QC symmetry (0.01 Å)
        sga_qc = SpacegroupAnalyzer(
            structure,
            symprec=constants.QC_SYMPREC,
            angle_tolerance=constants.ANGLE_TOLERANCE,
        )
        sd_qc = sga_qc.get_symmetry_dataset()
        if sd_qc is not None:
            ita_qc = int(sd_qc.number if hasattr(sd_qc, "number") else sd_qc["number"])
            out["qc_spacegroup_ita_0p01"] = str(ita_qc)
            ref_qc = spacegroup_reference.get(ita_qc, {})
            out["qc_hm_short_0p01"] = ref_qc.get("hm_short", "")
            if out["derived_spacegroup_ita"]:
                out["derived_symmetry_stable"] = str(ita == ita_qc)

        # Wyckoff positions from primary symmetry
        if sd is not None:
            try:
                # One entry per orbit; multiplicity refers to the conventional cell.
                orbit_counts = Counter(int(x) for x in sd.equivalent_atoms)
                cell_ratio = len(sd.std_types) / len(structure)
                wyk_mults = [
                    str(round(count * cell_ratio)) for count in orbit_counts.values()
                ]
                wyk_labels = [
                    f"{mult}{sd.wyckoffs[i]}"
                    for i, mult in zip(orbit_counts, wyk_mults)
                ]
                wyk_syms = [sd.site_symmetry_symbols[i] for i in orbit_counts]
                out["derived_wyckoff_labels"] = " ".join(wyk_labels)
                out["derived_wyckoff_multiplicities"] = " ".join(wyk_mults)
                out["derived_wyckoff_site_symmetries"] = " ".join(wyk_syms)
            except Exception as exc:
                LOG.debug("Wyckoff derivation failed: %s", exc)

        # Primitive / conventional standard cells
        if sd is not None:
            try:
                primitive = sga.get_primitive_standard_structure(
                    international_monoclinic=True, keep_site_properties=False
                )
                out.update(
                    _standard_cell_fields(primitive, "primitive", spacegroup_reference)
                )
            except Exception as exc:
                LOG.debug("Primitive cell failed: %s", exc)

            try:
                conventional = sga.get_conventional_standard_structure(
                    international_monoclinic=True, keep_site_properties=False
                )
                out.update(
                    _standard_cell_fields(
                        conventional, "conventional", spacegroup_reference
                    )
                )
                prefix = out["derived_bravais_lattice"]
                if prefix:
                    # AFLOW hR counts the rhombohedral cell, not its 3x hexagonal setting.
                    count = (
                        out.get("derived_primitive_nsites")
                        if prefix == "hR"
                        else len(conventional)
                    )
                    if count:
                        out["derived_pearson_atom_count"] = count
                        out["derived_pearson_cell_setting"] = (
                            "rhombohedral" if prefix == "hR" else "conventional"
                        )
                        out["derived_pearson_symbol"] = f"{prefix}{count}"
            except Exception as exc:
                LOG.debug("Conventional cell failed: %s", exc)

        out["derived_status"] = "ok"

    except Exception as exc:
        out["derived_status"] = "failed"
        out["derived_error"] = f"{type(exc).__name__}: {exc}"

    return out


def derive_dimensionality(structure) -> dict:
    """Measure bond-network dimensionality; layer symmetry needs an explicit open axis."""
    out = {
        "derived_dimensionality": None,
        "derived_dimensionality_status": "failed",
        "derived_dimensionality_method": "CrystalNN + Larsen",
        "derived_layer_group_ita": None,
        "derived_layer_group_status": "not_derivable",
    }
    try:
        from pymatgen.analysis.dimensionality import get_dimensionality_larsen
        from pymatgen.analysis.local_env import CrystalNN, MinimumDistanceNN

        parameters = {
            "distance_cutoffs": (0.5, 1),
            "x_diff_weight": 3.0,
            "porous_adjustment": True,
            "search_cutoff": 7,
        }
        neighbors = CrystalNN(**parameters)
        if not all(structure.lattice.pbc):
            # Voronoi/CrystalNN cannot construct a 3D hull for a flat open cell.
            parameters = {"tol": 0.1, "cutoff": 7}
            neighbors = MinimumDistanceNN(**parameters)
            out["derived_dimensionality_method"] = "MinimumDistanceNN + Larsen"
        out["derived_dimensionality_parameters"] = _compact_json(parameters)
        with warnings.catch_warnings(record=True) as caught:
            graph = neighbors.get_bonded_structure(structure)
        out["derived_dimensionality_warnings"] = _compact_json(
            sorted({str(w.message) for w in caught})
        )
        dim = int(get_dimensionality_larsen(graph))
        out["derived_dimensionality"] = dim
        out["derived_dimensionality_status"] = "ok"
        if dim != 2 and sum(structure.lattice.pbc) != 2:
            out["derived_layer_group_status"] = "not_applicable"
    except Exception as exc:
        out["derived_dimensionality_error"] = f"{type(exc).__name__}: {exc}"
    if sum(structure.lattice.pbc) == 2:
        try:
            import spglib

            axis = structure.lattice.pbc.index(False)
            cell = (
                structure.lattice.matrix,
                structure.frac_coords,
                [site.specie.Z for site in structure],
            )
            dataset = spglib.get_symmetry_layerdataset(
                cell, aperiodic_dir=axis, symprec=constants.PRIMARY_SYMPREC
            )
            if dataset is not None:
                out["derived_layer_group_ita"] = int(dataset.number)
                out["derived_layer_group_status"] = "ok"
        except Exception as exc:
            out["derived_layer_group_error"] = f"{type(exc).__name__}: {exc}"
    return out
