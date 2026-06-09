"""
smi_acquire.guidance
====================

"Guide the user to the right tool" -- a tiny rule engine that maps a few plain-language
answers about *what the user is doing* onto the A--O technique archetypes from
``_analysis/USE_CASE_TAXONOMY.md``.

Two entry points:

* :func:`questions` -- the decision-tree questions a wizard can present.
* :func:`recommend` -- given the user's answers (and/or free-text keywords), return a ranked
  list of ``(letter, score, reason)`` suggestions.

This is intentionally heuristic and transparent (every suggestion carries a human reason), not
a black box.  It is pure data, no GUI.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from . import techniques as _tech


# ---------------------------------------------------------------------------
# The guided questions (a front-end renders these as radio/checkbox groups)
# ---------------------------------------------------------------------------
QUESTIONS = [
    {
        "key": "control_variable",
        "prompt": "What are you primarily varying during the measurement?",
        "options": [
            ("photon_energy", "Photon energy (across an absorption edge)"),
            ("incident_angle", "Incident angle on a thin film (grazing)"),
            ("temperature", "Temperature"),
            ("humidity", "Humidity / solvent vapor"),
            ("potential", "Electrical potential / doping"),
            ("time", "Time (a process evolving)"),
            ("position", "Sample position (mapping a region)"),
            ("rotation", "Sample rotation angle (prs / phi)"),
            ("nothing", "Nothing -- static measurement of each sample"),
        ],
    },
    {
        "key": "geometry",
        "prompt": "What geometry?",
        "options": [
            ("transmission", "Transmission (capillary / well / solution / free film)"),
            ("grazing", "Grazing incidence (thin film on a substrate)"),
            ("specular", "Specular reflection (reflectivity)"),
        ],
    },
    {
        "key": "specialty",
        "prompt": "Any of these specialized goals? (optional)",
        "multi": True,
        "options": [
            ("cd_metrology", "Critical-dimension grating metrology (CD-SAXS)"),
            ("tomography", "Tomographic / texture reconstruction"),
            ("xpcs", "Coherent speckle / dynamics (XPCS)"),
            ("printing", "Follow an external 3D printer"),
            ("autonomous", "Let an ML/agent drive the experiment"),
            ("commissioning", "Beamline calibration / commissioning"),
            ("none", "None of these"),
        ],
    },
]


def questions():
    return QUESTIONS


# ---------------------------------------------------------------------------
# Scoring rules
# ---------------------------------------------------------------------------
# (answer_key, answer_value) -> list of (letter, weight, reason)
_RULES: Dict[Tuple[str, str], List[Tuple[str, int, str]]] = {
    ("control_variable", "photon_energy"): [("A", 5, "energy sweep across an edge")],
    ("control_variable", "incident_angle"): [("B", 5, "grazing incident-angle series")],
    ("control_variable", "temperature"): [("C", 5, "temperature is the control variable")],
    ("control_variable", "humidity"): [("G", 5, "humidity / SVA control")],
    ("control_variable", "potential"): [("H", 5, "applied potential / doping")],
    ("control_variable", "time"): [("F", 5, "time-resolved / kinetics")],
    ("control_variable", "position"): [("D", 5, "spatial mapping")],
    ("control_variable", "rotation"): [("K", 4, "rotation series (tomography/texture)"),
                                       ("I", 3, "prs rocking (if a grating)")],
    ("control_variable", "nothing"): [("E", 3, "static transmission measurement"),
                                      ("B", 2, "static grazing measurement")],

    ("geometry", "transmission"): [("E", 3, "transmission geometry"),
                                   ("A", 1, "edge scans are common in transmission")],
    ("geometry", "grazing"): [("B", 3, "grazing geometry"),
                              ("A", 1, "grazing edge scans use the energy machinery")],
    ("geometry", "specular"): [("J", 5, "specular reflectivity")],

    ("specialty", "cd_metrology"): [("I", 6, "CD-SAXS grating metrology")],
    ("specialty", "tomography"): [("K", 6, "tomography / texture")],
    ("specialty", "xpcs"): [("N", 6, "XPCS coherent bursts")],
    ("specialty", "printing"): [("L", 6, "external-printer-driven acquisition")],
    ("specialty", "autonomous"): [("M", 6, "autonomous / closed-loop")],
    ("specialty", "commissioning"): [("O", 6, "commissioning / calibration")],
}


def recommend(answers: Dict[str, object], *, keywords: str = "") -> List[Dict]:
    """Rank technique letters for the given answers.

    Parameters
    ----------
    answers : dict
        Maps question ``key`` -> selected value (str) or list of values (for multi).
    keywords : str
        Optional free text; matched against each technique's ``tags`` for a small boost.

    Returns
    -------
    list of dict, highest score first::

        [{"letter": "A", "score": 6, "title": ..., "reasons": [...]}, ...]
    """
    scores: Dict[str, int] = {}
    reasons: Dict[str, List[str]] = {}

    def add(letter, w, why):
        scores[letter] = scores.get(letter, 0) + w
        reasons.setdefault(letter, []).append(why)

    for key, val in (answers or {}).items():
        vals = val if isinstance(val, (list, tuple, set)) else [val]
        for v in vals:
            for letter, w, why in _RULES.get((key, str(v)), []):
                add(letter, w, why)

    kw = (keywords or "").lower().split()
    if kw:
        for letter in _tech.all_letters():
            spec = _tech.get(letter)
            tags = spec.tags if spec else _tech.SPECIAL.get(letter, {}).get("tags", [])
            hits = [k for k in kw if any(k in t for t in tags)]
            if hits:
                add(letter, len(hits), "keyword match: " + ", ".join(sorted(set(hits))))

    out = []
    for letter, score in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])):
        spec = _tech.get(letter)
        title = spec.title if spec else _tech.SPECIAL.get(letter, {}).get("title", letter)
        out.append({
            "letter": letter,
            "score": score,
            "title": title,
            "reasons": reasons.get(letter, []),
        })
    return out


__all__ = ["QUESTIONS", "questions", "recommend"]
