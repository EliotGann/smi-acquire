"""
smi_acquire.samples
====================

Single source of truth for the sample data model.

The whole point of ``smi_plans`` is the deliberate split between the *pure-Python*
sample model (:class:`Sample` / :class:`SampleList` in ``smi_plans._samples``) and the
device-dependent plan bodies.  A GUI only ever needs the pure-Python half, so this module
imports it directly -- it does **not** re-implement the dataclass (that would create a second
source of truth that silently drifts).

Resolution order
----------------
1. ``smi_plans`` already importable on ``sys.path`` (e.g. installed, or running inside the
   beamline env).
2. The templates directory discovered from ``$SMI_PLANS_PATH`` or the known on-disk default
   at NSLS-II, inserted on ``sys.path`` then imported.
3. A tiny vendored fallback (only the fields a GUI needs) so the apps still launch on a laptop
   with no beamline checkout.  The fallback is intentionally minimal and prints a warning.
"""

from __future__ import annotations

import os
import sys
import warnings

# Default on-disk location of the smi_plans templates package at the beamline / your repo.
_DEFAULT_TEMPLATES_DIRS = [
    os.environ.get("SMI_PLANS_PATH", ""),
    "/nsls2/users/egann/git/smi/scripts/SWAXS_user_scripts/templates",
    "/home/xf12id/SWAXS_user_scripts/templates",
]

#: Set once we know how a downstream script should reach smi_plans (used by codegen).
RESOLVED_TEMPLATES_PATH: str | None = None
USING_VENDORED_FALLBACK = False


def _try_import():
    global RESOLVED_TEMPLATES_PATH
    try:
        from smi_plans import Sample, SampleList  # type: ignore
        return Sample, SampleList
    except Exception:
        pass
    for d in _DEFAULT_TEMPLATES_DIRS:
        if d and os.path.isdir(os.path.join(d, "smi_plans")):
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                from smi_plans import Sample, SampleList  # type: ignore
                RESOLVED_TEMPLATES_PATH = d
                return Sample, SampleList
            except Exception:
                continue
    return None, None


Sample, SampleList = _try_import()


if Sample is None:  # ----------------------------------------------------------- fallback
    USING_VENDORED_FALLBACK = True
    warnings.warn(
        "smi_plans not found; using a minimal vendored Sample/SampleList. "
        "Set SMI_PLANS_PATH to the templates directory for the real model.",
        stacklevel=2,
    )
    from dataclasses import dataclass, field, asdict
    from typing import Any, Dict, List, Optional, Sequence

    def _f(v):
        return None if v in (None, "") else float(v)

    @dataclass
    class Sample:  # type: ignore[no-redef]
        name: str
        piezo_x: Optional[float] = None
        piezo_y: Optional[float] = None
        piezo_z: Optional[float] = None
        piezo_th: Optional[float] = None
        hexa_x: Optional[float] = None
        hexa_y: Optional[float] = None
        hexa_z: Optional[float] = None
        hexa_th: Optional[float] = None
        incident_angles: List[float] = field(default_factory=list)
        md: Dict[str, Any] = field(default_factory=dict)

        def __post_init__(self):
            for a in ("piezo_x", "piezo_y", "piezo_z", "piezo_th",
                      "hexa_x", "hexa_y", "hexa_z", "hexa_th"):
                setattr(self, a, _f(getattr(self, a)))
            self.incident_angles = [float(x) for x in self.incident_angles]
            if not self.name or not str(self.name).strip():
                raise ValueError("Sample.name must be a non-empty string")

        def to_dict(self):
            return asdict(self)

        @classmethod
        def from_dict(cls, d):
            return cls(**d)

    class SampleList:  # type: ignore[no-redef]
        def __init__(self, samples: Sequence["Sample"] = ()):
            self.samples = list(samples)
            self.validate()

        def __iter__(self):
            return iter(self.samples)

        def __len__(self):
            return len(self.samples)

        def __getitem__(self, i):
            return self.samples[i]

        def validate(self):
            names = [s.name for s in self.samples]
            if len(set(names)) != len(names):
                dupes = sorted({n for n in names if names.count(n) > 1})
                raise ValueError("Duplicate sample names: {}".format(dupes))
            return self

        @classmethod
        def from_dicts(cls, dicts):
            return cls([Sample.from_dict(d) for d in dicts])

        def to_dicts(self):
            return [s.to_dict() for s in self.samples]


#: The Sample fields a GUI table can edit, in display order.
SAMPLE_FIELDS = [
    "name",
    "piezo_x", "piezo_y", "piezo_z", "piezo_th",
    "hexa_x", "hexa_y", "hexa_z", "hexa_th",
    "incident_angles",
]

NUMERIC_FIELDS = {
    "piezo_x", "piezo_y", "piezo_z", "piezo_th",
    "hexa_x", "hexa_y", "hexa_z", "hexa_th",
}


def samples_to_records(samples):
    """SampleList -> list of flat dicts suitable for a Tabulator / table widget.

    ``incident_angles`` is flattened to a space-separated string; ``md`` is JSON-ish text.
    """
    import json
    recs = []
    for s in samples:
        d = s.to_dict()
        rec = {"name": d["name"]}
        for f in SAMPLE_FIELDS:
            if f in ("name", "incident_angles"):
                continue
            rec[f] = d.get(f)
        rec["incident_angles"] = " ".join(str(a) for a in d.get("incident_angles", []))
        md = d.get("md", {}) or {}
        rec["md"] = json.dumps(md) if md else ""
        recs.append(rec)
    return recs


def records_to_samples(records):
    """list of flat dicts (from a table) -> SampleList, tolerant of blanks/strings."""
    import json
    out = []
    for r in records:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        kw = {"name": name}
        for f in NUMERIC_FIELDS:
            v = r.get(f, None)
            if v in (None, ""):
                kw[f] = None
            else:
                kw[f] = float(v)
        ia = r.get("incident_angles", "") or ""
        if isinstance(ia, (list, tuple)):
            kw["incident_angles"] = [float(x) for x in ia]
        else:
            kw["incident_angles"] = [
                float(x) for x in str(ia).replace(";", " ").replace(",", " ").split()
            ]
        md_text = r.get("md", "") or ""
        if isinstance(md_text, dict):
            kw["md"] = md_text
        elif str(md_text).strip():
            try:
                kw["md"] = json.loads(md_text)
            except Exception:
                kw["md"] = {"note": str(md_text)}
        else:
            kw["md"] = {}
        out.append(Sample(**kw))
    return SampleList(out)


__all__ = [
    "Sample", "SampleList", "SAMPLE_FIELDS", "NUMERIC_FIELDS",
    "samples_to_records", "records_to_samples",
    "RESOLVED_TEMPLATES_PATH", "USING_VENDORED_FALLBACK",
]
