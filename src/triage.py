"""Recall triage: turn a recall's free text into an operational priority.

NHTSA publishes a do-not-drive flag (parkIt) and a park-outside flag
(parkOutSide), but none of the recalls affecting this fleet set either one.
Most consequences also mention a "risk of a crash", so that phrase separates
little. What does separate them is the mechanism of harm: a fuel leak near an
ignition source, or a van that rolls away in Park, is a different problem
from a rear camera that sometimes shows a blank image.

assess() is a pure function that returns a structured verdict. That is on
purpose: it is the seam where an LLM (an AIP Logic function, in Foundry
terms) could replace the rules. The input and output shapes stay the same,
the result is still only a recommendation, and a person still decides.

It fails safe. Text the rules can't classify is marked high priority for a
person to read today, never quietly filed as normal.
"""

import re

ENGINE = "rules-v2"

RANK = {"critical": 0, "high": 1, "normal": 2}

SLA_DAYS = {"critical": 2, "high": 7, "normal": 30, "review": 1}

# Recalls are matched on make, model and year, but each campaign only covers
# a range of VINs. Grounding every vehicle a make/model/year match flags would
# take most of the fleet off the road, so the first step is always to confirm
# the recall applies to this VIN.
RECOMMENDATION = {
    "critical": "Verify VIN today; ground if affected",
    "high": "Verify VIN; repair within 7 days",
    "normal": "Repair at next scheduled service",
    "review": "Read today: the triage rules couldn't classify this hazard",
}

# (priority, label, patterns, exceptions), most severe first. Every rule that
# matches is reported as a signal; the most severe one sets the priority.
RULES = [
    ("critical", "Fire risk",
     [r"\bfire\b", r"\bignit", r"melted wiring"],
     [r"extinguisher"]),
    ("critical", "Rollaway or unintended movement",
     [r"roll ?away", r"unintended (vehicle )?movement", r"move in an unexpected direction",
      r"move in a way that was not intended", r"not shift into .?park"],
     []),
    ("critical", "Loss of steering, braking or wheel control",
     [r"loss of (vehicle )?control", r"front wheel control", r"unintended steering",
      r"reduced (front )?brake function", r"loss of electronic brake",
      r"accelerat\w* despite", r"pedal from returning"],
     []),
    ("critical", "Tire failure at speed",
     [r"tread/belt loss", r"sudden air loss", r"blow ?out"],
     []),
    ("high", "Stall or loss of drive power while driving",
     [r"\bstall", r"loss of (drive )?(propulsion|power)\b(?!\s+steering)"],
     []),
    ("high", "Stability, anti-lock braking or steering assist degraded",
     [r"\bESC\b", r"\bABS\b", r"power steering assist"],
     []),
    ("high", "Occupant protection compromised in a crash",
     [r"air ?bag", r"seat[- ]?belt", r"restrain (an |the )?(seat )?occupant", r"properly restrain",
      r"seat back", r"structural integrity", r"unsecured (occupant|child|wheelchair)",
      r"side impact", r"child restraint"],
     []),
    ("high", "Unsecured cargo becoming a road hazard",
     [r"road hazard", r"cargo to fall"],
     []),
    ("normal", "Driver visibility or warning aid",
     [r"camera", r"rearview", r"rearward visibility", r"visibility of other drivers",
      r"telltale", r"daytime running", r"object behind the vehicle"],
     []),
    ("normal", "Labeling or on-board equipment",
     [r"label", r"extinguisher", r"overload"],
     []),
]

# Some defects are in paperwork, not parts. A door label listing the wrong
# tire size describes a blowout in its consequence, but the fix is a new
# label and a tire check, not grounding the vehicle.
COMPONENT_CAPS = [
    (r"\bLABELS?\b", "normal", "Labeling defect",
     "The defect is in the vehicle's label, not the part itself."),
]


def _snippet(text, match, radius=70):
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    body = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + body + ("…" if end < len(text) else "")


def _matches(text):
    found = []
    for level, label, patterns, exceptions in RULES:
        if any(re.search(e, text, re.IGNORECASE) for e in exceptions):
            continue
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                found.append((level, label, _snippet(text, match), match.group(0)))
                break
    return found


def assess(consequence, summary="", component="", park_it=False, park_outside=False):
    """Classify one recall. Returns a dict with the same shape an LLM would be asked for."""
    advisories = []
    if park_it:
        advisories.append(("critical", "NHTSA do-not-drive advisory",
                           "NHTSA marks this recall 'park it': do not drive until repaired.", "park it"))
    if park_outside:
        advisories.append(("critical", "NHTSA park-outside advisory",
                           "NHTSA advises parking outside and away from structures.", "park outside"))

    field = "consequence"
    text_hits = _matches(consequence or "")
    if not text_hits and summary:
        field = "summary"
        text_hits = _matches(summary)
    found = advisories + text_hits

    if not found:
        return {
            "priority": "high",
            "recommendedAction": RECOMMENDATION["review"],
            "rationale": "Unclassified hazard",
            "signals": [],
            "evidence": None,
            "evidenceField": None,
            "matchedPhrase": None,
            "needsReview": True,
            "slaDays": SLA_DAYS["review"],
            "engine": ENGINE,
        }

    found.sort(key=lambda hit: RANK[hit[0]])
    level, label, evidence, phrase = found[0]
    verdict = {
        "priority": level,
        "recommendedAction": RECOMMENDATION[level],
        "rationale": label,
        "signals": [hit[1] for hit in found],
        "evidence": evidence,
        "evidenceField": "nhtsa flags" if advisories and found[0] in advisories else field,
        "matchedPhrase": phrase,
        "needsReview": False,
        "slaDays": SLA_DAYS[level],
        "engine": ENGINE,
    }

    # NHTSA's own advisories are never capped.
    if not advisories:
        for pattern, cap, cap_label, reason in COMPONENT_CAPS:
            if re.search(pattern, component or "", re.IGNORECASE) and RANK[level] < RANK[cap]:
                verdict.update({
                    "priority": cap,
                    "recommendedAction": RECOMMENDATION[cap],
                    "rationale": cap_label,
                    "signals": [cap_label] + verdict["signals"],
                    "slaDays": SLA_DAYS[cap],
                    "cappedFrom": level,
                    "capReason": reason,
                })
                break
    return verdict
