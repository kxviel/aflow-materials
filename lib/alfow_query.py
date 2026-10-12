import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
OUTPUT = DATA_DIR / "aflow_full.json"
FAILURES = DATA_DIR / "aflow_failures.json"
FILES_DIR = DATA_DIR / "aflow_files"  # one folder per record: aflow_files/<auid>/<file>

LIMIT, PAGE_SIZE, MAX_PAGES, SAVE_EVERY = 100, 200, 5, 25
DELAY = 0.3  # seconds between requests
INDEX_URL = "https://aflow.org/API/aflux/?auid,aurl,paging({page},{size})"
ENDPOINTS = ("?format=json", "aflowlib.json")
SKIP_FILES = {"index.php"}  # server-generated directory listing, not data


def fetch_json(url):
    with urlopen(url, timeout=30) as response:
        return json.load(response)


def download_file(url, dest):
    """Stream to a .part file, then rename, so an interrupted download never looks finished."""
    part = dest.with_name(dest.name + ".part")
    with urlopen(url, timeout=30) as response, part.open("wb") as out:
        shutil.copyfileobj(response, out)
    os.replace(part, dest)


def base_url(entry):
    host, _, path = entry["aurl"].partition(":")
    return f"https://{host}/{path.strip('/')}/"


def fetch_material(entry):
    """Full record for an index entry, or None if no endpoint returns a valid one."""
    base = base_url(entry)

    for endpoint in ENDPOINTS:
        try:
            full = fetch_json(base + endpoint)
        except (OSError, ValueError):
            continue
        if isinstance(full, dict) and full.get("auid") == entry["auid"]:
            return {**entry, **full}
    return None


def download_files(full):
    """Download everything in the record's `files` list. Returns the names that failed."""
    base = base_url(full)
    folder = FILES_DIR / full["auid"].split(":")[-1]
    folder.mkdir(parents=True, exist_ok=True)
    failed = []

    for listed in full.get("files", []):
        name = Path(listed).name  # never write outside the record's folder
        dest = folder / name
        if name in SKIP_FILES or dest.exists():
            continue
        try:
            download_file(base + quote(name), dest)
        except OSError:
            failed.append(name)
            print(f"  FAIL {name}")
        time.sleep(DELAY)
    return failed


def save(path, data):
    """Atomic write: a crash mid-write can't corrupt the existing file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def fetch_materials(results, failures):
    """Stage 1: JSON record for every AFLUX index entry, up to LIMIT."""
    for page in range(1, MAX_PAGES + 1):
        print(f"Fetching AFLUX page {page}...")
        index = fetch_json(INDEX_URL.format(page=page, size=PAGE_SIZE))

        for entry in index.values():
            auid = entry["auid"]
            if len(results) >= LIMIT:
                break
            if auid in results:
                continue

            if full := fetch_material(entry):
                results[auid] = full
                print(f"[{len(results)}/{LIMIT}] {full.get('compound', auid)}")
                if len(results) % SAVE_EVERY == 0:
                    save(OUTPUT, results)
            else:
                failures[auid] = entry["aurl"]
                print(f"FAIL {auid}")
            time.sleep(DELAY)

        if len(results) >= LIMIT or len(index) < PAGE_SIZE:
            break


def download_all_files(results, failures):
    """Stage 2: files for every record. Files already on disk are skipped, so reruns resume."""
    for i, full in enumerate(results.values(), 1):
        print(f"files [{i}/{len(results)}] {full.get('compound', full['auid'])}")
        for name in download_files(full):
            failures[f"{full['auid']}/{name}"] = base_url(full) + name


def main():
    DATA_DIR.mkdir(exist_ok=True)

    # Keyed by AUID, so a rerun resumes where it stopped
    results = {}
    if OUTPUT.exists():
        results = {m["auid"]: m for m in json.loads(OUTPUT.read_text()).values()}
    failures = {}

    try:
        fetch_materials(results, failures)
        download_all_files(results, failures)
    finally:  # also runs on errors and Ctrl-C, so progress is kept
        save(OUTPUT, results)
        save(FAILURES, failures)

    print(
        f"Done: {len(results)}/{LIMIT} records, {len(failures)} failures -> {DATA_DIR}"
    )


if __name__ == "__main__":
    main()
