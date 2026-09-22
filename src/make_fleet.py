"""Generate the synthetic fleet roster for Bayline Logistics.

The recall data in this project is real. The fleet is not: no public dataset
lists a private company's vehicles, so this script fabricates one. It is
seeded, so the roster is identical on every machine.

The roster is deliberately imperfect. Roughly one row in six carries the kind
of damage a real spreadsheet accumulates: a nickname for the make, a model
written without its hyphen, stray whitespace, inconsistent casing. The
pipeline has to survive that, which is the point.
"""

import csv
import pathlib
import random

SEED = 7
OUT = pathlib.Path(__file__).resolve().parents[1] / "data" / "fleet.csv"

DEPOTS = ["Oakland", "Fresno", "Sacramento", "Reno"]

# (make, model, model_year, how many in the fleet)
FLEET_MIX = [
    ("Ford", "Transit", 2020, 5),
    ("Ford", "Transit", 2021, 3),
    ("Ford", "F-150", 2018, 4),
    ("Ford", "Escape", 2019, 3),
    ("Chevrolet", "Silverado 1500", 2019, 4),
    ("Chevrolet", "Express", 2018, 3),
    ("Ram", "ProMaster 1500", 2019, 4),
    ("Toyota", "RAV4", 2019, 4),
    ("Honda", "CR-V", 2017, 3),
    ("Nissan", "NV200", 2018, 3),
    ("Jeep", "Grand Cherokee", 2018, 4),
]

DRIVERS = [
    "A. Nguyen", "B. Okafor", "C. Delgado", "D. Whitfield", "E. Sandoval",
    "F. Bhatti", "G. Lindqvist", "H. Moreau", "I. Castellanos", "J. Pham",
    "K. Abara", "L. Ferreira", "M. Kaminski", "N. Haddad", "O. Sorensen",
    "P. Ravichandran", "Q. Baptiste", "R. Yoon", "S. Mbeki", "T. Kovacs",
]

# How a make or model gets mistyped when a depot manager is in a hurry.
MAKE_TYPOS = {"Chevrolet": "Chevy", "Ford": "FORD ", "Toyota": " toyota"}
MODEL_TYPOS = {"F-150": "F150", "CR-V": "CRV", "Silverado 1500": "Silverado",
               "ProMaster 1500": "ProMaster", "RAV4": "RAV 4"}


def vin(rng: random.Random) -> str:
    """A VIN-shaped string. Not a valid VIN: the check digit is meaningless."""
    alphabet = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"
    return "".join(rng.choice(alphabet) for _ in range(17))


def plate(rng: random.Random) -> str:
    return f"{rng.randint(1, 9)}{''.join(rng.choice('ABCDEFGHJKLMNPRSTUVWXYZ') for _ in range(3))}{rng.randint(100, 999)}"


def main() -> None:
    rng = random.Random(SEED)
    rows = []
    counter = 0
    dirty = 0

    for make, model, year, count in FLEET_MIX:
        for _ in range(count):
            counter += 1
            damaged = rng.random() < 0.25
            if damaged and (make in MAKE_TYPOS or model in MODEL_TYPOS):
                dirty += 1

            rows.append({
                "vehicle_id": f"VEH-{counter:03d}",
                "vin": vin(rng),
                "make": MAKE_TYPOS.get(make, make) if damaged else make,
                "model": MODEL_TYPOS.get(model, model) if damaged else model,
                "model_year": year,
                "depot": rng.choice(DEPOTS),
                "plate": plate(rng),
                "odometer_km": rng.randrange(18_000, 240_000, 137),
                "assigned_driver": rng.choice(DRIVERS),
                "status": "in_service",
            })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} vehicles to {OUT}")
    print(f"  {dirty} rows carry a messy make or model spelling")


if __name__ == "__main__":
    main()
