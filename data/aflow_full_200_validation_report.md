# AFLOW full 200: coverage and validation audit

Date: 2026-10-09

## Outcome

All 200 records have valid input geometry and successful pipeline derivation. The saved intermediary preserves every original source record, and both outputs retain all 200 unique AUIDs in their input order. The current files contain 152 distinct source keys and 174 distinct merged keys (including three export metadata fields) across the dataset.

The current pipeline records 32 warning events across 17 materials, with no processing errors. Formula and volume comparisons agree for all 200 records. This audit does not certify all 152 source properties or change the pipeline parameters.

## Implementation update

The default command was run for all 200 materials after implementation. Their
original raw records, existing normalized crystallography, independently derived
values, consistency flags and warning lists match the previous run. Quality status
is `processed` for 183 records and `review_required` for 17; none is marked validated.
The normalizer now computes `quality_status` for both CSV and intermediary JSON;
the AFLOW wrapper exports that decision as `quality.status`.

The schema now has 166 canonical CSV fields, including raw and normalized columns
for the 16 documented scalar properties and stoichiometry. Merged JSON adds
`quality`, `provenance` and `units`; raw records remain stored once in the intermediary.
Missing gaps remain null for 191 materials, and the five reported zero gaps remain zero.

The Rh source review, broader tolerance-policy benchmarking and physical bonding-model
validation remain open. The implementation does not change tolerances or clear warnings.

## Current workspace

- `field_map.yaml` contains an AFLOW mapping.
- `aflow.py` calls `normalize_row` with geometry enabled.
- Normalization and both JSON writes are active.
- `_orig` fields remain distinct and unchanged in the merged output; the independent structure uses relaxed `geometry` and relaxed `positions_fractional`.
- AFLOW documents both geometry/coordinate fields as relaxed; the local `aflow_properties.txt` contains those definitions.
- The default `.venv/bin/aflow` command now targets the expanded 200-record dataset and was executed successfully.

The earlier-version statements in the pasted review do not describe the active pipeline.

## Property coverage

| Property | Reported | Normalized | Independently calculated | Current comparison |
|---|---:|---:|---:|---|
| Formula / chemistry | 200 | 200 | 200 | Reduced composition agrees for 200 |
| Space-group number | 200 | 200 | 200 | 186 agree; 14 disagree at current primary settings |
| Crystal system | 200 | 200 | 200 | 187 agree; 13 disagree |
| Point group (HM) | 200 | 200 | 200 | 186 agree; 14 disagree in this audit |
| Bravais lattice | 200 | 200 | 200 | 189 agree; 11 disagree in this audit |
| Full Pearson symbol | 200 | 200 | 200 | 189 agree; 11 differ, including 5 count disagreements |
| Pearson atom count | 200 (symbol suffix) | 200 | 200 | 195 agree; 5 disagree at current primary settings |
| Six lattice parameters | 200 | 200 | 200 (from the same input geometry) | Structural parsing succeeds; this is not independent measurement |
| Simulation-cell volume | 200 | 200 | 200 | All 200 agree within pipeline tolerance |
| Fractional coordinates | 200 | 200 | Used by derivation | Shape, finiteness and site counts valid for 200 |
| Per-site species | 200 through species + composition | 200 | Used by derivation | Expanded from reported counts; no independent species assignment check |
| Simulation site count | 200 | 200 | 200 | Matches input coordinate count for 200 |
| Wyckoff descriptors | 200 | 200 | 200 | Reported and derived values retained; no complete species/orbit correspondence validation |
| Physical dimensionality | 0 | 0 | 200 | 185 × 3D, 13 × 2D, 1 × 1D, 1 × 0D; model-dependent |
| Material form | 0 | 0 | Not calculated | Missing |
| Explicit periodic boundary conditions | 0 | 0 | Assumed 3-periodic for 200 | An assumption, recorded in intermediary metadata |
| Layer group | 0 | 0 | 0 | 187 not applicable; 13 not derivable without an explicit open axis |
| Lattice matrix | 0 | 0 | 200 | Reconstructed orientation from six parameters |
| Primitive/conventional site counts | No dedicated source fields used | Not normalized | 200 | Cell definitions retained in derived metadata |
| Documented energies / gaps / density / pressures / stoichiometry | See inventory below | 16 scalar fields plus stoichiometry | Not recalculated | Finite numeric values; pressure kbar → GPa; definitions remain distinct |
| Other property families | See inventory below | Retained as supplied | Not recalculated | No undocumented unit assumptions |

All 200 bonding calculations captured model warnings. Physical dimensionality is an estimate under the recorded CrystalNN + Larsen model, not an AFLOW-reported value.

## Geometry and cell conventions

No `_orig` geometry is fed into the relaxed-structure comparison. Pairwise atom distances from the reconstructed lattice and reported fractional coordinates agree with reported Cartesian coordinates for all 200 materials within 0.0001 Å; the largest difference is 0.0000144827 Å. This checks the observed coordinate geometry up to rotation and translation; it does not establish every unobserved lattice direction from the Cartesian arrays.

The Pearson warning compares against the conventional cell, not the simulation or primitive cell. The 13 AFLOW `hR` entries are handled using their rhombohedral count; the corresponding hexagonal conventional cells contain three times as many sites. Their counts all agree in this dataset.

## Space-group investigation

Current primary settings are `symprec=0.1 Å, angle_tolerance=5°`. Current QC uses `symprec=0.01 Å` with the same 5° angle tolerance. This audit additionally tested 0.001 and 0.0001 Å at 5°, plus automatic angle handling (`angle_tolerance=-1`) and 0.1° at selected distance tolerances.

Thirteen of the fourteen original discrepancies can reproduce AFLOW under the alternative settings tested. The saved primary results and warnings remain unchanged. Holding angle tolerance at 5° explains why distance-only tightening does not reconcile every record.

| AUID | Compound | AFLOW relaxed | Current primary | Current QC | Audit 0.01 Å / automatic angle | Interpretation |
|---|---|---:|---:|---:|---:|---|
| `aflow:000004dec6c1b4d1` | Hf2Nb1V1 | 12 | 71 | 12 | 12 | Tolerance-sensitive; agreement found |
| `aflow:0000320afe07746e` | Mn1Pb2Pd1 | 10 | 123 | 10 | 10 | Tolerance-sensitive; agreement found |
| `aflow:00004f5182f2658a` | Ga1Mo2Y1 | 10 | 47 | 47 | 10 | Tolerance-sensitive; agreement found |
| `aflow:0000f4dfa0fa7abc` | Bi1In2Ru1 | 65 | 123 | 123 | 65 | Tolerance-sensitive; agreement found |
| `aflow:00016f052f674347` | Ag2Li1Sc1 | 10 | 123 | 10 | 10 | Tolerance-sensitive; agreement found |
| `aflow:000245cfbb4ff3d6` | Cr1La2 | 164 | 191 | 164 | 164 | Tolerance-sensitive; agreement found |
| `aflow:0002919fb74aedb9` | Bi1In1P1 | 42 | 107 | 42 | 42 | Tolerance-sensitive; agreement found |
| `aflow:0002fe33bdd3a957` | Al1Ge2Se1 | 25 | 99 | 25 | 25 | Tolerance-sensitive; agreement found |
| `aflow:0003267835d261b4` | Al1As2Se1 | 99 | 123 | 99 | 99 | Tolerance-sensitive; agreement found |
| `aflow:000341e280c8e1de` | Cr2Os1Se1 | 6 | 38 | 6 | 6 | Tolerance-sensitive; agreement found |
| `aflow:00039eaaf08ca42b` | Br2Co1Li1 | 6 | 38 | 6 | 6 | Tolerance-sensitive; agreement found |
| `aflow:0003a06dba970f7b` | Ag1Sb1Tl2 | 12 | 65 | 65 | 12 | Tolerance-sensitive; agreement found |
| `aflow:000406fabb5831ed` | Al1Ba1Na1 | 156 | 187 | 156 | 156 | Tolerance-sensitive; agreement found |
| `aflow:000493c698497af9` | Rh6 | 123 | 221 | 221 | 166 | Unresolved |

Across all 200 materials, the alternative 0.01 Å / automatic-angle setting matches AFLOW for 194. It resolves 13 original disagreements but creates five new disagreements among formerly matching records. Therefore no fixed parameter change is justified solely by making the original warnings disappear.

| Additional disagreement under the alternative setting | Compound | AFLOW | Alternative |
|---|---|---:|---:|
| `aflow:0002420c93308ccf` | As1Li2Pt1 | 216 | 44 |
| `aflow:000265acf1616d10` | In2Mg1Na1 | 99 | 35 |
| `aflow:000362b7ce0bca33` | Os2Ru2 | 51 | 11 |
| `aflow:00039971bcf486b8` | Hf2Tl1V1 | 99 | 35 |
| `aflow:0003cd1b6c70c45e` | As1Re1Sn2 | 123 | 47 |

## Pearson count investigation

| AUID | Compound | AFLOW Pearson | Input sites | Primary primitive | Primary conventional | Audit conventional at 0.01 Å / automatic angle | Interpretation |
|---|---|---|---:|---:|---:|---:|---|
| `aflow:0000f4dfa0fa7abc` | Bi1In2Ru1 | oS8 | 4 | 4 | 4 | 8 | Count agrees with AFLOW under alternative symmetry |
| `aflow:0002919fb74aedb9` | Bi1In1P1 | oF12 | 3 | 3 | 6 | 12 | Count agrees with AFLOW under alternative symmetry |
| `aflow:000341e280c8e1de` | Cr2Os1Se1 | mP4 | 4 | 4 | 8 | 4 | Count agrees with AFLOW under alternative symmetry |
| `aflow:00039eaaf08ca42b` | Br2Co1Li1 | mP4 | 4 | 4 | 8 | 4 | Count agrees with AFLOW under alternative symmetry |
| `aflow:000493c698497af9` | Rh6 | tP2 | 6 | 1 | 1 | 3 | Unresolved; count depends on symmetry and supercell reduction |

Four of five count disagreements reconcile under alternative symmetry settings. These are not evidence of a simple conventional-versus-primitive comparison bug.

## Remaining Rh record

`aflow:000493c698497af9` (`Rh6`) reports SG 123 and Pearson `tP2`. Its six-atom relaxed geometry gives SG 221 at current primary settings, SG 166 at 0.01 Å with automatic angle handling, and SG 156 at 0.001 Å. Its other AFLOW fields also contain SG 166/221 in `sg`/`sg2`, and the relaxed prototype label is `A_tP1_123_a`. The record needs source-level review; these results do not establish an AFLOW error.

An attempt to fetch its original `CONTCAR.relax` could not complete in this environment (DNS resolution failure; web access also failed). The available evidence is the downloaded JSON and reconstructed geometry. This prevents certification of that source record.

## Production-readiness findings

1. Define and benchmark a symmetry tolerance policy across representative datasets. Keep tolerance-sensitive disagreements distinguishable from invalid geometry.
2. Review the Rh source structure and metadata before marking it validated.
3. Implemented: merged exports carry quality checks/issues, field provenance and a reference to the intermediary file, joined by AUID.
4. Implemented for documented scalar families: energy quantities retain eV/atom or eV/cell, gaps retain eV, pressure converts kbar → GPa, and density retains g/cm³. Per-cell quantities retain the reported simulation-cell basis. Reported stoichiometry is validated. Undocumented families remain reported values; no unit is guessed for CCE fields absent from the local registry.
5. Preserve explicit missing/assumed status for PBC, material form and layer groups; validate the bonding model and missing oxidation-state/radius behavior before treating physical dimensions as authoritative.
6. Implemented: the default command now targets `aflow_full_200.json`.

## Detailed field coverage

Nonempty means a field is neither null, an empty string, `unknown`, nor an empty container. Numeric zero is present. Method/status columns are counted structurally; populated metadata does not imply that the associated physical value exists. Blank documented units below mean the local property registry does not specify a unit.

### Raw AFLOW field inventory

| Field | Present | Nonempty | Null/empty | Absent | JSON types | Documented units |
|---|---:|---:|---:|---:|---|---|
| `Bravais_lattice_lattice_system` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Bravais_lattice_lattice_system_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Bravais_lattice_lattice_type` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Bravais_lattice_lattice_type_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Bravais_lattice_lattice_variation_type` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Bravais_lattice_lattice_variation_type_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Bravais_lattice_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Bravais_lattice_relax` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Bravais_superlattice_lattice_system` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Bravais_superlattice_lattice_system_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Bravais_superlattice_lattice_type` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Bravais_superlattice_lattice_type_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Bravais_superlattice_lattice_variation_type` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Bravais_superlattice_lattice_variation_type_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Egap` | 9 | 9 | 0 | 191 | float: 4, int: 5 | eV |
| `Egap_fit` | 9 | 9 | 0 | 191 | float: 4, int: 5 | eV |
| `Egap_type` | 9 | 9 | 0 | 191 | str: 9 | — |
| `PV_atom` | 200 | 200 | 0 | 0 | int: 200 | eV/atom |
| `PV_cell` | 200 | 200 | 0 | 0 | int: 200 | eV/cell |
| `Pearson_symbol_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Pearson_symbol_relax` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Pearson_symbol_superlattice` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Pearson_symbol_superlattice_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `Pulay_stress` | 200 | 200 | 0 | 0 | int: 200 | kbar |
| `Wyckoff_letters` | 200 | 200 | 0 | 0 | list: 200 | — |
| `Wyckoff_letters_orig` | 200 | 200 | 0 | 0 | list: 200 | — |
| `Wyckoff_multiplicities` | 200 | 200 | 0 | 0 | list: 200 | — |
| `Wyckoff_multiplicities_orig` | 200 | 200 | 0 | 0 | list: 200 | — |
| `Wyckoff_site_symmetries` | 200 | 200 | 0 | 0 | list: 200 | — |
| `Wyckoff_site_symmetries_orig` | 200 | 200 | 0 | 0 | list: 200 | — |
| `aflow_prototype_label_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `aflow_prototype_label_relax` | 200 | 200 | 0 | 0 | str: 200 | — |
| `aflow_prototype_params_list_orig` | 200 | 200 | 0 | 0 | list: 200 | — |
| `aflow_prototype_params_list_relax` | 200 | 200 | 0 | 0 | list: 200 | — |
| `aflow_prototype_params_values_orig` | 200 | 200 | 0 | 0 | list: 200 | — |
| `aflow_prototype_params_values_relax` | 200 | 200 | 0 | 0 | list: 200 | — |
| `aflow_version` | 191 | 191 | 0 | 9 | str: 191 | — |
| `aflowlib_date` | 200 | 200 | 0 | 0 | list: 23, str: 177 | — |
| `aflowlib_version` | 200 | 200 | 0 | 0 | str: 200 | — |
| `auid` | 200 | 200 | 0 | 0 | str: 200 | — |
| `aurl` | 200 | 200 | 0 | 0 | str: 200 | — |
| `bader_atomic_volumes` | 10 | 10 | 0 | 190 | list: 10 | &Aring;<sup>3</sup> |
| `bader_net_charges` | 10 | 10 | 0 | 190 | list: 10 | e<sup>-</sup> |
| `calculation_cores` | 200 | 200 | 0 | 0 | int: 200 | — |
| `calculation_memory` | 200 | 200 | 0 | 0 | float: 134, int: 66 | MB |
| `calculation_time` | 200 | 200 | 0 | 0 | float: 194, int: 6 | seconds |
| `catalog` | 200 | 200 | 0 | 0 | str: 200 | — |
| `code` | 200 | 200 | 0 | 0 | str: 200 | — |
| `composition` | 200 | 200 | 0 | 0 | list: 200 | — |
| `compound` | 200 | 200 | 0 | 0 | str: 200 | — |
| `crystal_class` | 200 | 200 | 0 | 0 | str: 200 | — |
| `crystal_class_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `crystal_family` | 200 | 200 | 0 | 0 | str: 200 | — |
| `crystal_family_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `crystal_system` | 200 | 200 | 0 | 0 | str: 200 | — |
| `crystal_system_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `data_api` | 200 | 200 | 0 | 0 | str: 200 | — |
| `data_source` | 200 | 200 | 0 | 0 | str: 200 | — |
| `delta_electronic_energy_convergence` | 200 | 200 | 0 | 0 | float: 200 | eV |
| `delta_electronic_energy_threshold` | 200 | 200 | 0 | 0 | float: 200 | eV |
| `density` | 200 | 200 | 0 | 0 | float: 200 | g/cm<sup>3</sup> |
| `density_orig` | 200 | 200 | 0 | 0 | float: 200 | — |
| `dft_type` | 200 | 200 | 0 | 0 | list: 200 | — |
| `eentropy_atom` | 200 | 200 | 0 | 0 | float: 200 | eV/atom |
| `eentropy_cell` | 200 | 200 | 0 | 0 | float: 200 | eV/cell |
| `energy_atom` | 200 | 200 | 0 | 0 | float: 200 | eV/atom |
| `energy_cell` | 200 | 200 | 0 | 0 | float: 200 | eV/cell |
| `energy_cutoff` | 200 | 200 | 0 | 0 | float: 144, int: 56 | eV |
| `enthalpy_atom` | 200 | 200 | 0 | 0 | float: 200 | eV/atom |
| `enthalpy_cell` | 200 | 200 | 0 | 0 | float: 200 | eV/cell |
| `enthalpy_formation_atom` | 197 | 197 | 0 | 3 | float: 197 | eV/atom |
| `enthalpy_formation_cce_0K_atom` | 2 | 2 | 0 | 198 | float: 2 | — |
| `enthalpy_formation_cce_0K_cell` | 2 | 2 | 0 | 198 | float: 2 | — |
| `enthalpy_formation_cce_300K_atom` | 2 | 2 | 0 | 198 | float: 2 | — |
| `enthalpy_formation_cce_300K_cell` | 2 | 2 | 0 | 198 | float: 2 | — |
| `enthalpy_formation_cell` | 197 | 197 | 0 | 3 | float: 197 | eV/cell |
| `entropic_temperature` | 197 | 197 | 0 | 3 | float: 190, int: 7 | K |
| `files` | 200 | 200 | 0 | 0 | list: 200 | — |
| `forces` | 200 | 200 | 0 | 0 | list: 200 | eV/&Aring; |
| `geometry` | 200 | 200 | 0 | 0 | list: 200 | — |
| `geometry_orig` | 200 | 200 | 0 | 0 | list: 200 | — |
| `kpoints` | 200 | 200 | 0 | 0 | list: 200 | — |
| `kpoints_bands_nkpts` | 34 | 34 | 0 | 166 | int: 34 | — |
| `kpoints_bands_path` | 34 | 34 | 0 | 166 | list: 34 | — |
| `kpoints_relax` | 200 | 200 | 0 | 0 | list: 200 | — |
| `kpoints_static` | 34 | 34 | 0 | 166 | list: 34 | — |
| `lattice_system_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `lattice_system_relax` | 200 | 200 | 0 | 0 | str: 200 | — |
| `lattice_variation_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `lattice_variation_relax` | 200 | 200 | 0 | 0 | str: 200 | — |
| `ldau_TLUJ` | 4 | 4 | 0 | 196 | list: 4 | — |
| `ldau_j` | 4 | 4 | 0 | 196 | list: 4 | eV |
| `ldau_l` | 4 | 4 | 0 | 196 | list: 4 | — |
| `ldau_type` | 7 | 7 | 0 | 193 | int: 7 | — |
| `ldau_u` | 4 | 4 | 0 | 196 | list: 4 | eV |
| `loop` | 200 | 200 | 0 | 0 | list: 200 | — |
| `natoms` | 200 | 200 | 0 | 0 | int: 200 | — |
| `natoms_orig` | 200 | 200 | 0 | 0 | int: 200 | — |
| `nbondxx` | 200 | 200 | 0 | 0 | list: 200 | &Aring; |
| `node_CPU_Cores` | 157 | 157 | 0 | 43 | int: 157 | — |
| `node_CPU_MHz` | 157 | 157 | 0 | 43 | int: 157 | MHz |
| `node_CPU_Model` | 157 | 157 | 0 | 43 | str: 157 | — |
| `node_RAM_GB` | 157 | 157 | 0 | 43 | int: 157 | Gb |
| `nspecies` | 200 | 200 | 0 | 0 | int: 200 | — |
| `point_group_Hermann_Mauguin` | 200 | 200 | 0 | 0 | str: 200 | — |
| `point_group_Hermann_Mauguin_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `point_group_Schoenflies` | 200 | 200 | 0 | 0 | str: 200 | — |
| `point_group_Schoenflies_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `point_group_orbifold` | 200 | 200 | 0 | 0 | str: 200 | — |
| `point_group_orbifold_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `point_group_order` | 200 | 200 | 0 | 0 | int: 200 | — |
| `point_group_order_orig` | 200 | 200 | 0 | 0 | int: 200 | — |
| `point_group_structure` | 200 | 200 | 0 | 0 | str: 200 | — |
| `point_group_structure_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `point_group_type` | 200 | 200 | 0 | 0 | str: 200 | — |
| `point_group_type_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `positions_cartesian` | 200 | 200 | 0 | 0 | list: 200 | &Aring; |
| `positions_fractional` | 200 | 200 | 0 | 0 | list: 200 | — |
| `pressure` | 200 | 200 | 0 | 0 | int: 200 | kbar |
| `pressure_residual` | 200 | 200 | 0 | 0 | float: 197, int: 3 | kbar |
| `prototype` | 200 | 200 | 0 | 0 | str: 200 | — |
| `reciprocal_geometry` | 200 | 200 | 0 | 0 | list: 200 | — |
| `reciprocal_geometry_orig` | 200 | 200 | 0 | 0 | list: 200 | — |
| `reciprocal_lattice_type` | 200 | 200 | 0 | 0 | str: 200 | — |
| `reciprocal_lattice_type_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `reciprocal_lattice_variation_type` | 200 | 200 | 0 | 0 | str: 200 | — |
| `reciprocal_lattice_variation_type_orig` | 200 | 200 | 0 | 0 | str: 200 | — |
| `reciprocal_volume_cell` | 200 | 200 | 0 | 0 | float: 200 | &Aring;<sup>-3</sup> |
| `reciprocal_volume_cell_orig` | 200 | 200 | 0 | 0 | float: 200 | &Aring;<sup>-3</sup> |
| `scintillation_attenuation_length` | 200 | 200 | 0 | 0 | float: 200 | cm |
| `sg` | 200 | 200 | 0 | 0 | list: 200 | — |
| `sg2` | 200 | 200 | 0 | 0 | list: 200 | — |
| `spacegroup_orig` | 200 | 200 | 0 | 0 | int: 200 | — |
| `spacegroup_relax` | 200 | 200 | 0 | 0 | int: 200 | — |
| `species` | 200 | 200 | 0 | 0 | list: 200 | — |
| `species_pp` | 200 | 200 | 0 | 0 | list: 200 | — |
| `species_pp_AUID` | 200 | 200 | 0 | 0 | list: 200 | — |
| `species_pp_ZVAL` | 200 | 200 | 0 | 0 | list: 200 | e<sup>-</sup> |
| `species_pp_version` | 200 | 200 | 0 | 0 | list: 200 | — |
| `spinD` | 200 | 200 | 0 | 0 | list: 200 | &mu;<sub>B</sub> |
| `spinF` | 34 | 34 | 0 | 166 | float: 3, int: 31 | &mu;<sub>B</sub>/cell |
| `spin_atom` | 200 | 200 | 0 | 0 | float: 57, int: 143 | &mu;<sub>B</sub>/atom |
| `spin_cell` | 200 | 200 | 0 | 0 | float: 55, int: 145 | &mu;<sub>B</sub>/cell |
| `stoichiometry` | 200 | 200 | 0 | 0 | list: 200 | — |
| `stress_tensor` | 200 | 200 | 0 | 0 | list: 200 | kbar |
| `title` | 6 | 6 | 0 | 194 | str: 6 | — |
| `valence_cell_iupac` | 200 | 200 | 0 | 0 | int: 200 | — |
| `valence_cell_std` | 200 | 200 | 0 | 0 | int: 200 | — |
| `volume_atom` | 200 | 200 | 0 | 0 | float: 200 | &Aring;<sup>3</sup>/atom |
| `volume_atom_orig` | 200 | 200 | 0 | 0 | float: 200 | — |
| `volume_cell` | 200 | 200 | 0 | 0 | float: 200 | &Aring;<sup>3</sup> |
| `volume_cell_orig` | 200 | 200 | 0 | 0 | float: 199, int: 1 | — |

### Normalized field coverage

| Field | Nonempty | Missing/empty |
|---|---:|---:|
| `normalized_spacegroup_ita` | 200 | 0 |
| `normalized_spacegroup_notation_detected` | 200 | 0 |
| `normalized_spacegroup_hm_short` | 200 | 0 |
| `normalized_spacegroup_hm_full` | 200 | 0 |
| `normalized_spacegroup_hall` | 200 | 0 |
| `normalized_spacegroup_schoenflies` | 200 | 0 |
| `normalized_crystal_system` | 200 | 0 |
| `normalized_bravais_lattice` | 200 | 0 |
| `normalized_pearson_symbol` | 200 | 0 |
| `normalized_pearson_atom_count` | 200 | 0 |
| `normalized_lattice_a` | 200 | 0 |
| `normalized_lattice_b` | 200 | 0 |
| `normalized_lattice_c` | 200 | 0 |
| `normalized_lattice_alpha` | 200 | 0 |
| `normalized_lattice_beta` | 200 | 0 |
| `normalized_lattice_gamma` | 200 | 0 |
| `normalized_lattice_volume` | 200 | 0 |
| `normalized_lattice_matrix` | 0 | 200 |
| `normalized_site_species` | 200 | 0 |
| `normalized_site_frac_coords` | 200 | 0 |
| `normalized_nsites` | 200 | 0 |
| `normalized_pbc` | 0 | 200 |
| `normalized_geometry_status` | 200 | 0 |
| `normalized_point_group_schoenflies` | 200 | 0 |
| `normalized_point_group_hm` | 200 | 0 |
| `normalized_formula_hill` | 200 | 0 |
| `normalized_formula_reduced` | 200 | 0 |
| `normalized_formula_anonymous` | 200 | 0 |
| `normalized_composition_json` | 200 | 0 |
| `normalized_chemical_system` | 200 | 0 |
| `normalized_nelements` | 200 | 0 |
| `normalized_dimensionality` | 0 | 200 |
| `normalized_dimensionality_status` | 200 | 0 |
| `normalized_material_form` | 0 | 200 |
| `normalized_layer_group_ita` | 0 | 200 |
| `normalized_layer_group_status` | 200 | 0 |
| `normalized_wyckoff_labels` | 200 | 0 |
| `normalized_wyckoff_multiplicities` | 200 | 0 |
| `normalized_wyckoff_site_symmetries` | 200 | 0 |
| `normalized_wyckoff_status` | 200 | 0 |
| `normalized_energy_atom` | 200 | 0 |
| `normalized_energy_cell` | 200 | 0 |
| `normalized_enthalpy_atom` | 200 | 0 |
| `normalized_enthalpy_cell` | 200 | 0 |
| `normalized_enthalpy_formation_atom` | 197 | 3 |
| `normalized_enthalpy_formation_cell` | 197 | 3 |
| `normalized_eentropy_atom` | 200 | 0 |
| `normalized_eentropy_cell` | 200 | 0 |
| `normalized_PV_atom` | 200 | 0 |
| `normalized_PV_cell` | 200 | 0 |
| `normalized_Egap` | 9 | 191 |
| `normalized_Egap_fit` | 9 | 191 |
| `normalized_density` | 200 | 0 |
| `normalized_pressure` | 200 | 0 |
| `normalized_pressure_residual` | 200 | 0 |
| `normalized_Pulay_stress` | 200 | 0 |
| `normalized_stoichiometry` | 200 | 0 |

### Derived field coverage

| Field | Nonempty | Missing/empty |
|---|---:|---:|
| `derived_formula` | 200 | 0 |
| `derived_reduced_formula` | 200 | 0 |
| `derived_formula_hill` | 200 | 0 |
| `derived_formula_anonymous` | 200 | 0 |
| `derived_composition_json` | 200 | 0 |
| `derived_chemical_system` | 200 | 0 |
| `derived_nelements` | 200 | 0 |
| `derived_nsites` | 200 | 0 |
| `derived_spacegroup_ita` | 200 | 0 |
| `derived_spacegroup_hm_short` | 200 | 0 |
| `derived_spacegroup_hm_full` | 200 | 0 |
| `derived_spacegroup_hall` | 200 | 0 |
| `derived_spacegroup_schoenflies` | 200 | 0 |
| `derived_crystal_system` | 200 | 0 |
| `derived_point_group_schoenflies` | 200 | 0 |
| `derived_point_group_hm` | 200 | 0 |
| `derived_bravais_lattice` | 200 | 0 |
| `derived_pearson_symbol` | 200 | 0 |
| `derived_pearson_atom_count` | 200 | 0 |
| `derived_pearson_cell_setting` | 200 | 0 |
| `derived_primitive_nsites` | 200 | 0 |
| `derived_conventional_nsites` | 200 | 0 |
| `derived_lattice_matrix` | 200 | 0 |
| `derived_lattice_matrix_method` | 200 | 0 |
| `derived_symprec` | 200 | 0 |
| `derived_qc_symprec` | 200 | 0 |
| `derived_angle_tolerance` | 200 | 0 |
| `derived_periodic_dimension` | 200 | 0 |
| `derived_pbc_status` | 200 | 0 |
| `derived_dimensionality` | 200 | 0 |
| `derived_dimensionality_status` | 200 | 0 |
| `derived_dimensionality_method` | 200 | 0 |
| `derived_dimensionality_parameters` | 200 | 0 |
| `derived_dimensionality_warnings` | 200 | 0 |
| `derived_dimensionality_error` | 0 | 200 |
| `derived_layer_group_ita` | 0 | 200 |
| `derived_layer_group_status` | 200 | 0 |
| `derived_layer_group_error` | 0 | 200 |
| `derived_wyckoff_labels` | 200 | 0 |
| `derived_wyckoff_multiplicities` | 200 | 0 |
| `derived_wyckoff_site_symmetries` | 200 | 0 |
| `derived_primitive_spacegroup_ita` | 200 | 0 |
| `derived_primitive_hm_short` | 200 | 0 |
| `derived_conventional_spacegroup_ita` | 200 | 0 |
| `derived_conventional_hm_short` | 200 | 0 |
| `qc_spacegroup_ita_0p01` | 200 | 0 |
| `qc_hm_short_0p01` | 200 | 0 |
| `derived_symmetry_stable` | 200 | 0 |
| `derived_lattice_a` | 200 | 0 |
| `derived_lattice_b` | 200 | 0 |
| `derived_lattice_c` | 200 | 0 |
| `derived_lattice_alpha` | 200 | 0 |
| `derived_lattice_beta` | 200 | 0 |
| `derived_lattice_gamma` | 200 | 0 |
| `derived_lattice_volume` | 200 | 0 |
| `derived_status` | 200 | 0 |
| `derived_error` | 0 | 200 |

## Reproducibility and sources

The initial audit left source and outputs unchanged. The subsequent implementation updated the documented quantity rules, default input path and export metadata, and regenerated both output files. The JSON companion retains the original audit hashes as well as current file hashes, dependency versions, all tolerance sweeps and per-field counts.

- [AFLOW property documentation](https://www.aflow.org/documentation/) and the downloaded `aflow_properties.txt` define relaxed versus original fields and units.
- [Spglib 2.7 symmetry API](https://spglib.readthedocs.io/en/v2.7.0/api/autodoc/spglib.spg.html) documents length and angle tolerances and automatic angle handling.
- `aflow_full_200.json`, `aflow_full_200_intermediary.json`, and `aflow_full_200_normalized.json` are the data evidence.
