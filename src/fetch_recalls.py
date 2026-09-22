"""Pull safety recall campaigns from NHTSA for every vehicle type in the fleet.

Source: https://api.nhtsa.gov/recalls/recallsByVehicle (public, no key required)

One request per distinct make/model/year in the roster. Responses are saved
untouched, with a manifest recording each request. Nothing downstream reads
the network: the pipeline only reads these files, so a rebuild is
reproducible and the raw response stays available as evidence.

The fetch is all or nothing. Responses are staged in a temporary folder and
moved into data/raw/ only if every request succeeded. If any request fails,
data/raw/ is left exactly as it was and the script exits non-zero. A failed
request must never look like "this vehicle has no recalls".
"""

import csv
import http.client
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from normalize import canonical_make, canonical_model, slug  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
FLEET = ROOT / "data" / "fleet.csv"
RAW = ROOT / "data" / "raw"
ENDPOINT = "https://api.nhtsa.gov/recalls/recallsByVehicle"
PAUSE_SECONDS = 0.4

# URLError, HTTPError, timeouts and connection resets are all OSErrors; a
# truncated body is an HTTPException; a bad body is a ValueError.
FETCH_ERRORS = (OSError, http.client.HTTPException, ValueError)


def fleet_vehicle_types():
    """Distinct (make, model, year) in the roster, canonicalized and sorted."""
    types = set()
    with FLEET.open(newline="") as handle:
        for row in csv.DictReader(handle):
            make, model, year = ((row.get(c) or "").strip() for c in ("make", "model", "model_year"))
            # Skip rows the pipeline rejects (it reports them), so one bad row
            # can't produce a request that fails and blocks every refresh.
            if not (make and model and year):
                continue
            try:
                year = str(int(year))
            except ValueError:
                continue
            types.add((canonical_make(make), canonical_model(make, model), year))
    return sorted(types)


def fetch(make, model, year):
    query = urllib.parse.urlencode({"make": make, "model": model, "modelYear": year})
    url = f"{ENDPOINT}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "recall-ops/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("response has no 'results' list")
    return url, payload


def swap_into_place(staging, target):
    """Replace target with staging by renaming. A reader that lands in the
    moment between the two renames finds no data/raw/ and stops; it never
    reads a mix of old and new files."""
    previous = target.with_name(target.name + ".previous")
    if previous.exists():
        shutil.rmtree(previous)
    if target.exists():
        target.rename(previous)
    staging.rename(target)
    if previous.exists():
        shutil.rmtree(previous)


def main():
    types = fleet_vehicle_types()
    RAW.parent.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(tempfile.mkdtemp(prefix=".raw-staging-", dir=RAW.parent))
    manifest, failures, total_records = [], [], 0

    try:
        for make, model, year in types:
            name = f"recalls_{slug(make, model, year)}.json"
            try:
                url, payload = fetch(make, model, year)
            except FETCH_ERRORS as error:
                failures.append(f"{make} {model} {year}: {error}")
                print(f"  !! {make} {model} {year}: {error}")
                continue

            (staging / name).write_text(json.dumps(payload, indent=2))
            count = len(payload["results"])
            total_records += count
            manifest.append({
                "file": f"data/raw/{name}",
                "requested": {"make": make, "model": model, "model_year": year},
                "url": url,
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "record_count": count,
            })
            print(f"  {make} {model} {year}: {count} campaigns -> {name}")
            time.sleep(PAUSE_SECONDS)

        if failures:
            print(f"\n{len(failures)} of {len(types)} requests failed. data/raw/ was left unchanged.")
            return 1

        (staging / "_manifest.json").write_text(json.dumps(manifest, indent=2))
        os.chmod(staging, 0o755)  # mkdtemp creates 0700; data/raw/ should read like any other folder
        swap_into_place(staging, RAW)
        staging = None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    print(f"\n{len(manifest)} vehicle types fetched, {total_records} raw recall records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
