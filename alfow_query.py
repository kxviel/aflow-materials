import json
import time
from pathlib import Path
from urllib.request import urlopen

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

OUTPUT = DATA_DIR / "aflow_full_200.json"
FAILURES = DATA_DIR / "aflow_failures.json"

LIMIT = 200
PAGE_SIZE = 200
MAX_PAGES = 5


def fetch_json(url):
    with urlopen(url, timeout=30) as response:
        content = response.read()

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON from {url}: {content[:100]!r}") from e


def get_full_material(material):
    host, _, path = material["aurl"].partition(":")
    base = f"https://{host}/{path.strip('/')}/"

    for endpoint in ("?format=json", "aflowlib.json"):
        try:
            full = fetch_json(base + endpoint)

            if not isinstance(full, dict):
                raise TypeError("Expected JSON object")

            if full.get("auid") != material["auid"]:
                raise ValueError("AUID mismatch")

            return {**material, **full}

        except (OSError, ValueError):
            continue

    raise RuntimeError("Both REST endpoints failed")


def save(path, data):
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


results = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {}
failures = {}

for page in range(1, MAX_PAGES + 1):
    if len(results) >= LIMIT:
        break

    print(f"\nFetching AFLUX page {page}...")

    url = f"https://aflow.org/API/aflux/?auid,aurl,paging({page},{PAGE_SIZE})"

    index = fetch_json(url)

    if not isinstance(index, dict) or not index:
        raise ValueError("AFLUX returned no material index")

    for label, material in index.items():
        if len(results) >= LIMIT:
            break

        if label in results:
            continue

        try:
            full = get_full_material(material)

            results[label] = full
            save(OUTPUT, results)

            print(
                f"[{len(results)}/{LIMIT}] "
                f"OK {full['compound']} "
                f"({len(full)} properties)"
            )

        except (OSError, ValueError, RuntimeError) as e:
            failures[label] = {
                "auid": material.get("auid"),
                "error": str(e),
            }
            print(f"FAIL {material.get('compound')}: {e}")

        time.sleep(0.5)

save(FAILURES, failures)

print(f"\nDownloaded {len(results)}/{LIMIT} materials")
print(f"Saved to {OUTPUT}")
