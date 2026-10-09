# AFLOW: Downloading and Preparing Materials Data

*Presentation content — simple English*

---

## Slide 1 — Project Goal

**Goal:** Collect materials data from AFLOW and prepare it for our materials database.

Our work has three parts:
- **Download** the material information.
- **Normalize** the data so it follows the same format.
- **Derive** extra information from the crystal structure.

We are starting with **200 materials**. The long-term goal is to handle **millions of materials**.

---

## Slide 2 — What Is AFLOW?

**AFLOW** stands for **Automatic FLOW for Materials Discovery**.

- It is a tool and database for materials science.
- It runs many computer simulations automatically.
- It mainly uses **Density Functional Theory (DFT)** to study materials.
- It stores information about crystal structures and material properties.
- Researchers use it to find and compare materials.

**Main point:** Most AFLOW property values come from **computer calculations**, not lab tests.

---

## Slide 3 — How Much Data Is There?

- AFLOW contains **millions of material entries**.
- Our earlier API response reported **2,981,853 entries**.
- The AFLOW website has reported a larger total (around **3.9 million**). The number can change, and the two counts may cover different sets of records.
- A single material may have **many properties**, but not every material has every property.

Examples of stored information:
- Chemical formula and elements
- Crystal structure and atom positions
- Energy and band gap
- Mechanical and thermal properties, when calculated
- Details about the simulation

---

## Slide 4 — How Is AFLOW Data Organized?

Every material has an entry with its own ID and properties.

- **AUID:** Unique ID of the AFLOW entry
- **AURL:** Address used to find the entry
- **Catalog:** Group the entry belongs to, such as `ICSD`, `LIB1`, `LIB2`, or `LIB3`

Our downloaded AFLOW property list contains:

| Property status | Number |
|---|---:|
| Total documented properties | 193 |
| Marked as `production` | 175 |
| Production properties with a documented request syntax | 171 |

**Important:** These are property definitions, **not** a guarantee that each material has 171 values.

Examples of property names:
- `compound` — chemical formula
- `spacegroup_relax` — space-group number
- `geometry` — cell lengths and angles
- `positions_fractional` — atom positions
- `Egap` — electronic band gap
- `energy_atom` — calculated energy per atom

---

## Slide 5 — License and Data Use

- The **AFLOW software** is available under **GPL-3.0**.
- GPL-3.0 is a free and open-source software license.
- **The database data may have different terms from the software.**
- We must check AFLOW's data-use rules before sharing the full dataset or using it commercially.
- We should cite AFLOW and its scientific papers when using the data.

**Main point:** An open-source program does **not** automatically mean all its database contents have the same license.

---

## Slide 6 — Is the Data Experimental or Computational?

**Most AFLOW properties are computational.** They are calculated using simulations.

| Type | What it means |
|---|---|
| Computational data | Values predicted by computer simulations, such as DFT |
| Experimental structure source | A crystal structure originally reported from experiments, for example through ICSD |
| Hypothetical structure | A possible material structure that may not have been made in a lab |

**Can we count how many materials are experimentally proven?**

**Not yet.** AFLOW does not give us a simple, reliable number for this in the records we have studied.

Also:
- An ICSD-based starting structure does **not** mean all of AFLOW's calculated properties were measured in a lab.
- A predicted stable material is **not automatically** a material that has been made experimentally.
- We would need to compare AFLOW with experimental databases and research papers.

---

## Slide 7 — How Do We Download the Data?

Our Python script uses **two API steps**:

**Step 1: Get a list of materials from AFLUX**

```text
https://aflow.org/API/aflux/?auid,aurl,paging(1,200)
```

This gives us the IDs and addresses of materials.

**Step 2: Get the complete information for each material**

The script uses the material's `aurl` and tries:

```text
https://<material-address>/?format=json
```

If that fails, it tries the material's `aflowlib.json` file.

**Step 3: Check and save**

- Check that the downloaded `auid` matches the requested material.
- Save all fields returned by the API.
- Record failed downloads.

Our script is set up to collect **200 successful entries**, looking across up to five pages.

---

## Slide 8 — What Are Raw, Normalized and Derived Data?

We keep three kinds of data separate:

| Type | Simple meaning | Example |
|---|---|---|
| **Raw** | Exactly what AFLOW provided | `"compound": "Al1"` |
| **Normalized** | The same information in a standard format | Formula written as `Al` |
| **Derived** | New information calculated from existing data | Recalculated space group or cell volume |

**Why?**
- We do not want to lose the original values.
- We want data from different databases to use the same format.
- We want to compare reported values with calculated values to find possible problems.

---

## Slide 9 — What Have We Implemented for Normalization?

We have written code for these steps:

**1. Map AFLOW fields to common names**
- `auid` → material ID
- `compound` → formula
- `spacegroup_relax` → space group
- `Pearson_symbol_relax` → Pearson symbol
- `geometry` → six cell lengths and angles

**2. Clean and standardize values**
- Chemical formulas
- Space groups and crystal systems
- Bravais lattices and Pearson symbols
- Point groups
- Wyckoff information

**3. Calculate extra structural information**
- Lattice matrix and cell volume
- Crystal symmetry
- Primitive and conventional cell details
- Wyckoff positions and structural dimensionality, where possible

**4. Check results**
- Compare reported and calculated values.
- Record missing fields, errors and mismatches.

**Libraries used:** `pymatgen`, `spglib`, Python and YAML configuration.

---

## Slide 10 — What Our Code Produces

Our normalization adapter keeps the results in separate sections:

```text
Material entry
├── auid
├── raw
├── normalized
├── derived
├── consistency
└── issues
```

- **raw:** Original AFLOW data
- **normalized:** Standardized values
- **derived:** Extra calculated values
- **consistency:** Whether reported and calculated values agree
- **issues:** Warnings or errors

It can also create a merged record for use in our database.

**Current status:** The downloader and normalization code are written. We still need to confirm that the full 200-material dataset downloads and normalizes correctly.

---

## Slide 11 — How Can We Process All 3 Million+ Materials?

The same basic download method can work, but we need to improve the system.

**Problems with the current 200-material script:**
- It repeatedly rewrites one JSON file.
- It keeps downloaded results in memory.
- A very large download will take a long time.
- We need stronger tracking of progress and failures.

**Plan for millions of materials:**
1. Fetch material IDs in pages.
2. Download complete metadata for each entry.
3. Save records **one at a time or in small batches** using JSONL or a database.
4. Use **SQLite** to track downloaded, pending and failed materials.
5. Add retries, checkpoints and safe download speed limits.
6. Normalize the data in batches without loading everything into RAM.
7. Check whether AFLOW offers an official bulk download or approved high-volume access.

**Main point:** We should not store millions of entries in one huge JSON object.

---

## Slide 12 — Progress and Next Steps

| Task | Status |
|---|---|
| Understand AFLOW and its data structure | Done |
| Find how to request full material metadata | Done |
| Download a complete example entry | Done |
| Write the 200-material downloader | Code written |
| Write AFLOW field mappings and normalization code | Code written |
| Test and verify all 200 normalized entries | Still needed |
| Build the million-record download pipeline | Planned |

**Next steps:**
- Test the downloader on 200 materials.
- Count successful and failed downloads.
- Check which properties are usually missing.
- Run the normalization code on the downloaded data.
- Check the results and fix any errors.
- Then move to a larger dataset.

**Conclusion:** We are building a system that turns AFLOW's raw computational data into clear, consistent and reusable material records.

---

## References

- AFLOW website: https://aflow.org/
- AFLOW API: https://aflow.org/API/aflux/
- AFLOW source code and software license: https://github.com/aflow-org/aflow
- Taylor et al. (2014), *A RESTful API for exchanging materials data in the AFLOWLIB.org consortium*, Computational Materials Science, 93, 178–192. https://doi.org/10.1016/j.commatsci.2014.05.014
- Project code: `alfow_query.py`, `field_map.yaml`, `aflow.py`, `normalize_reported_values.py`, `derive.py`, `constants.py`

*Note: Database sizes depend on the date and query. The experimental-versus-computational breakdown has not been measured in this project.*
