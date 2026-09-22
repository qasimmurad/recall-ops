"""Canonical names for vehicle makes and models.

The fleet spreadsheet and the NHTSA API disagree about spelling. The
spreadsheet holds whatever a depot manager typed ("Chevy", "F150", "crv"),
while the API only answers to its own vocabulary ("CHEVROLET", "F-150",
"CR-V"). Every join between the two sides goes through this module so the
mismatch is handled in exactly one place.
"""

import re

MAKE_ALIASES = {
    "CHEVY": "CHEVROLET",
    "CHEV": "CHEVROLET",
    "GMC TRUCK": "GMC",
    "VW": "VOLKSWAGEN",
    "MERCEDES": "MERCEDES-BENZ",
    "DODGE RAM": "RAM",
}

# Keyed by (canonical make, squashed model) so "SIERRA" under GMC and
# "SIERRA" under some other make can diverge later without surprises.
MODEL_ALIASES = {
    ("FORD", "F150"): "F-150",
    ("FORD", "F 150"): "F-150",
    ("FORD", "TRANSIT VAN"): "TRANSIT",
    ("HONDA", "CRV"): "CR-V",
    ("HONDA", "CR V"): "CR-V",
    ("CHEVROLET", "SILVERADO"): "SILVERADO 1500",
    ("CHEVROLET", "EXPRESS VAN"): "EXPRESS",
    # Fleet records carry the trim ("ProMaster 1500"); NHTSA files every
    # ProMaster van under plain "PROMASTER" and rejects the trim with a 400.
    ("RAM", "PROMASTER 1500"): "PROMASTER",
    ("RAM", "PROMASTER 2500"): "PROMASTER",
    ("TOYOTA", "RAV 4"): "RAV4",
    ("JEEP", "GRAND CHEROKEE WK"): "GRAND CHEROKEE",
}


def squash(value: str) -> str:
    """Upper-case, trim, and collapse runs of whitespace and punctuation noise."""
    if value is None:
        return ""
    text = str(value).strip().upper()
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def canonical_make(raw_make: str) -> str:
    make = squash(raw_make)
    return MAKE_ALIASES.get(make, make)


def canonical_model(raw_make: str, raw_model: str) -> str:
    make = canonical_make(raw_make)
    model = squash(raw_model)
    return MODEL_ALIASES.get((make, model), model)


def vehicle_key(raw_make: str, raw_model: str, model_year) -> str:
    """The join key shared by fleet rows and recall records."""
    year = squash(model_year)
    return f"{canonical_make(raw_make)}|{canonical_model(raw_make, raw_model)}|{year}"


def slug(*parts) -> str:
    """Filesystem-safe identifier, used for the raw JSON file names."""
    text = "_".join(squash(p) for p in parts).lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")
