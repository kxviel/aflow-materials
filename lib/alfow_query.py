import json
import os
import time
from pathlib import Path
from urllib.request import urlopen

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
OUTPUT = DATA_DIR / "aflow_full.json"
FAILURES = DATA_DIR / "aflow_failures.json"

LIMIT, PAGE_SIZE, MAX_PAGES, SAVE_EVERY = 100, 200, 5, 25
INDEX_URL = "https://aflow.org/API/aflux/?auid,aurl,paging({page},{size})"
ENDPOINTS = ("?format=json", "aflowlib.json")


def fetch_json(url):
    with urlopen(url, timeout=30) as response:
        return json.load(response)


def fetch_material(entry):
    """Full record for an index entry, or None if no endpoint returns a valid one."""
    host, _, path = entry["aurl"].partition(":")
    base = f"https://{host}/{path.strip('/')}/"

    for endpoint in ENDPOINTS:
        try:
            full = fetch_json(base + endpoint)
        except (OSError, ValueError):
            continue
        if isinstance(full, dict) and full.get("auid") == entry["auid"]:
            return {**entry, **full}
    return None


def save(path, data):
    """Atomic write: a crash mid-write can't corrupt the existing file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def main():
    DATA_DIR.mkdir(exist_ok=True)

    # Keyed by AUID, so a rerun resumes where it stopped
    results = {}
    if OUTPUT.exists():
        results = {m["auid"]: m for m in json.loads(OUTPUT.read_text()).values()}
    failures = {}

    try:
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
                time.sleep(0.3)

            if len(results) >= LIMIT or len(index) < PAGE_SIZE:
                break
    finally:  # also runs on errors and Ctrl-C, so progress is kept
        save(OUTPUT, results)
        save(FAILURES, failures)

    print(f"Downloaded {len(results)}/{LIMIT}, {len(failures)} failed -> {OUTPUT}")


if __name__ == "__main__":
    main()
