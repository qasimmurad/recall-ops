"""Build the fleet's ontology objects from the raw inputs.

Inputs (never modified):
  data/fleet.csv            the roster, as the depots maintain it
  data/raw/*.json           NHTSA responses, one file per vehicle type
  data/raw/_manifest.json   when and how each of those files was fetched

Outputs:
  state/objects.json          Vehicle, RecallCampaign and WorkOrder objects
  state/pipeline_report.json  what the run found, fixed, merged and rejected
  state/first_seen.json       the date each work order was first opened

The output is a pure function of the inputs plus one piece of history: the
first-seen ledger. The pipeline only ever adds to it, so a work order keeps
its original open date across rebuilds, even if it drops out of a run and
comes back. Deleting state/ deletes that history.

Ingestion fails closed. If any vehicle type in the roster has no NHTSA
response, the run stops and writes nothing, because "never fetched" must
not look like "no recalls".

Human decisions are not stored here. They live in state/edits.jsonl and the
kernel replays them on top of this output, so a rebuild never erases them.
"""

import csv
import json
import os
import pathlib
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from normalize import canonical_make, canonical_model, squash, vehicle_key  # noqa: E402
from triage import assess  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
FLEET = ROOT / "data" / "fleet.csv"
RAW = ROOT / "data" / "raw"
MANIFEST = RAW / "_manifest.json"
STATE = ROOT / "state"
OBJECTS = STATE / "objects.json"
REPORT = STATE / "pipeline_report.json"
FIRST_SEEN = STATE / "first_seen.json"

REQUIRED_COLUMNS = ("vehicle_id", "make", "model", "model_year")
STATUSES = {"in_service", "grounded"}


class Stop(Exception):
    """The inputs can't be trusted. Nothing gets written."""


def clean(text):
    return " ".join((text or "").split()) or None


def cell(row, column):
    return (row.get(column) or "").strip()


def write_atomic(path, data):
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temp.write_text(json.dumps(data, indent=2))
    os.replace(temp, path)


def parse_nhtsa_date(value):
    """NHTSA sends dates as DD/MM/YYYY strings. Returns ISO YYYY-MM-DD, or None."""
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date().isoformat()
    except (AttributeError, ValueError):
        return None


def normalize_status(raw):
    value = re.sub(r"[\s-]+", "_", (raw or "").strip().lower())
    return value if value in STATUSES else None


def load_vehicles(report):
    vehicles = {}
    with FLEET.open(newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):  # row 1 is the header
            vehicle_id = squash(row.get("vehicle_id"))
            try:
                missing = [c for c in REQUIRED_COLUMNS if not cell(row, c)]
                if missing:
                    raise ValueError(f"missing {', '.join(missing)}")
                if vehicle_id in vehicles:
                    raise ValueError(f"duplicate vehicle_id {vehicle_id}")
                model_year = int(cell(row, "model_year"))
                odometer = int(cell(row, "odometer_km")) if cell(row, "odometer_km") else None
            except ValueError as error:
                report["rejected"].append({"file": "data/fleet.csv", "row": row_number, "why": str(error)})
                continue

            raw_make, raw_model = row["make"], row["model"]
            make = canonical_make(raw_make)
            model = canonical_model(raw_make, raw_model)
            aliased = squash(raw_make) != make or squash(raw_model) != model
            padded = raw_make != raw_make.strip() or raw_model != raw_model.strip()
            if aliased or padded:
                report["normalized"].append({
                    "row": row_number,
                    "vehicleId": vehicle_id,
                    "entered": f"{raw_make!r} / {raw_model!r}",
                    "canonical": f"{make} / {model}",
                })

            status = normalize_status(row.get("status") or "in_service")
            if status is None:
                # For a safety roster, the safe assumption about an unknown
                # status is that the vehicle is on the road.
                status = "in_service"
                report["statusDefaulted"].append({"row": row_number, "vehicleId": vehicle_id,
                                                  "entered": row.get("status")})

            vehicles[vehicle_id] = {
                "vehicleId": vehicle_id,
                "vin": squash(row.get("vin")) or None,
                "make": make,
                "model": model,
                "modelYear": model_year,
                "vehicleKey": vehicle_key(raw_make, raw_model, model_year),
                "depot": cell(row, "depot") or "Unassigned",
                "plate": squash(row.get("plate")) or None,
                "odometerKm": odometer,
                "assignedDriver": cell(row, "assigned_driver") or None,
                "status": status,
                "groundedReason": None,
                "statusChangedAt": None,
                "_source": {
                    "file": "data/fleet.csv",
                    "row": row_number,
                    "entered": {"make": raw_make, "model": raw_model},
                },
            }
    return vehicles


def load_manifest():
    try:
        manifest = json.loads(MANIFEST.read_text())
    except (OSError, ValueError) as error:
        raise Stop(f"can't read {MANIFEST.relative_to(ROOT)} ({error}). Run python3 src/fetch_recalls.py.") from None
    if not isinstance(manifest, list):
        raise Stop(f"{MANIFEST.relative_to(ROOT)} is not a list of fetches.")
    return manifest


def missing_vehicle_types(vehicles, manifest):
    """Vehicle types in the roster with no NHTSA response in the manifest."""
    fetched = {
        vehicle_key(e["requested"]["make"], e["requested"]["model"], e["requested"]["model_year"])
        for e in manifest
    }
    return sorted({v["vehicleKey"] for v in vehicles.values()} - fetched)


def load_campaigns(manifest, report):
    campaigns = {}

    for entry in manifest:
        requested = entry["requested"]
        # The manifest records what we asked NHTSA for. That, not the record's
        # own Make/Model text, is what ties a recall to a vehicle type.
        key = vehicle_key(requested["make"], requested["model"], requested["model_year"])
        try:
            payload = json.loads((ROOT / entry["file"]).read_text())
            results = payload["results"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise Stop(f"can't read {entry['file']} ({error}). Run python3 src/fetch_recalls.py.") from None

        for index, record in enumerate(results):
            report["rawRecords"] += 1
            number = squash(record.get("NHTSACampaignNumber"))
            if not number:
                report["rejected"].append({"file": entry["file"], "record": index, "why": "missing campaign number"})
                continue

            own_key = vehicle_key(record.get("Make"), record.get("Model"), record.get("ModelYear"))
            if own_key != key:
                report["recordKeyMismatches"].append({"campaign": number, "requested": key, "record": own_key})

            source = {"file": entry["file"], "record": index, "url": entry["url"], "fetchedAt": entry["fetched_at"]}

            if number in campaigns:
                campaign = campaigns[number]
                if key not in campaign["coveredVehicleTypes"]:
                    campaign["coveredVehicleTypes"].append(key)
                campaign["_sources"].append(source)
                report["duplicatesMerged"] += 1
                continue

            received = parse_nhtsa_date(record.get("ReportReceivedDate"))
            if received is None:
                report["dateParseFailures"].append({"campaign": number, "value": record.get("ReportReceivedDate")})

            component = clean(record.get("Component")) or "UNKNOWN"
            verdict = assess(
                record.get("Consequence") or "",
                record.get("Summary") or "",
                component=component,
                park_it=record.get("parkIt") is True,
                park_outside=record.get("parkOutSide") is True,
            )
            campaigns[number] = {
                "campaignNumber": number,
                "manufacturer": clean(record.get("Manufacturer")),
                "component": component,
                "componentGroup": component.split(":")[0].strip(),
                "summary": clean(record.get("Summary")),
                "consequence": clean(record.get("Consequence")),
                "remedy": clean(record.get("Remedy")),
                "notes": clean(record.get("Notes")),
                "reportReceived": received,
                "parkIt": record.get("parkIt") is True,
                "parkOutside": record.get("parkOutSide") is True,
                "overTheAir": record.get("overTheAirUpdate") is True,
                "coveredVehicleTypes": [key],
                "priority": verdict["priority"],
                "triage": verdict,
                "_sources": [source],
            }
    return campaigns


def build_work_orders(vehicles, campaigns, first_seen, report):
    today = date.today().isoformat()
    by_type = defaultdict(list)
    for campaign in campaigns.values():
        for key in campaign["coveredVehicleTypes"]:
            by_type[key].append(campaign)

    work_orders = {}
    for vehicle in vehicles.values():
        matches = by_type.get(vehicle["vehicleKey"], [])
        if not matches:
            report["vehiclesWithoutRecalls"].append(vehicle["vehicleId"])

        for campaign in matches:
            order_id = f"WO-{vehicle['vehicleId']}-{campaign['campaignNumber']}"
            opened = first_seen.get(order_id)
            if opened is None:
                opened = first_seen[order_id] = today
                report["workOrdersNew"] += 1
            else:
                report["workOrdersCarriedForward"] += 1
            due = date.fromisoformat(opened) + timedelta(days=campaign["triage"]["slaDays"])

            work_orders[order_id] = {
                "workOrderId": order_id,
                "vehicleId": vehicle["vehicleId"],
                "campaignNumber": campaign["campaignNumber"],
                "priority": campaign["priority"],
                "recommendedAction": campaign["triage"]["recommendedAction"],
                "status": "open",
                "openedAt": opened,
                "dueBy": due.isoformat(),
                "scheduledFor": None,
                "shop": None,
                "completedOn": None,
                "completionNotes": None,
                "dismissalReason": None,
                "dismissalEvidence": None,
                "lastActionAt": None,
                "_derivedFrom": {
                    "rule": "vehicle.vehicleKey is in campaign.coveredVehicleTypes",
                    "vehicleKey": vehicle["vehicleKey"],
                },
            }
    return work_orders


def main():
    report = {
        "rawRecords": 0,
        "duplicatesMerged": 0,
        "workOrdersNew": 0,
        "workOrdersCarriedForward": 0,
        "normalized": [],
        "rejected": [],
        "statusDefaulted": [],
        "dateParseFailures": [],
        "recordKeyMismatches": [],
        "vehiclesWithoutRecalls": [],
    }

    try:
        vehicles = load_vehicles(report)
        manifest = load_manifest()
        missing = missing_vehicle_types(vehicles, manifest)
        if missing:
            raise Stop(f"no NHTSA response for {len(missing)} vehicle type(s) in the roster: {', '.join(missing)}. "
                       "Run python3 src/fetch_recalls.py.")
        campaigns = load_campaigns(manifest, report)
    except Stop as stop:
        print(f"Stopped: {stop}\nNothing was written.", file=sys.stderr)
        return 1

    first_seen = json.loads(FIRST_SEEN.read_text()) if FIRST_SEEN.exists() else {}
    work_orders = build_work_orders(vehicles, campaigns, first_seen, report)

    by_priority = defaultdict(int)
    for campaign in campaigns.values():
        by_priority[campaign["priority"]] += 1
    needs_review = sum(c["triage"]["needsReview"] for c in campaigns.values())
    capped = sum("cappedFrom" in c["triage"] for c in campaigns.values())

    STATE.mkdir(parents=True, exist_ok=True)
    # The ledger goes first. If the run dies between writes, the ledger may hold
    # a date that was never served, but it never misses one that was.
    write_atomic(FIRST_SEEN, first_seen)
    write_atomic(OBJECTS, {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ontology": "baylineFleetSafety",
        "objects": {"Vehicle": vehicles, "RecallCampaign": campaigns, "WorkOrder": work_orders},
    })
    write_atomic(REPORT, report)

    print("Pipeline complete")
    print(f"  vehicles     {len(vehicles)} loaded, {len(report['rejected'])} rejected, "
          f"{len(report['normalized'])} make/model spellings normalized")
    for rejected in report["rejected"]:
        print(f"    rejected {rejected['file']} {rejected.get('row', rejected.get('record'))}: {rejected['why']}")
    if report["statusDefaulted"]:
        print(f"    {len(report['statusDefaulted'])} unrecognized status value(s) treated as in service")
    print(f"  recalls      {report['rawRecords']} raw records -> {len(campaigns)} campaigns "
          f"({report['duplicatesMerged']} duplicates merged across vehicle types)")
    print(f"  dates        {len(report['dateParseFailures'])} failed to parse")
    print(f"  triage       {by_priority['critical']} critical, {by_priority['high']} high, "
          f"{by_priority['normal']} normal ({needs_review} unclassified and sent for a human read, "
          f"{capped} capped as labeling defects)")
    print(f"  work orders  {len(work_orders)} ({report['workOrdersNew']} new, "
          f"{report['workOrdersCarriedForward']} carried forward)")
    if report["vehiclesWithoutRecalls"]:
        print(f"  no recalls   {', '.join(report['vehiclesWithoutRecalls'])}")
    if report["recordKeyMismatches"]:
        print(f"  mismatches   {len(report['recordKeyMismatches'])} records name a different vehicle than requested")
    print(f"  -> {OBJECTS.relative_to(ROOT)}, {REPORT.relative_to(ROOT)}, {FIRST_SEEN.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
