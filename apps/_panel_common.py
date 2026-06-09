"""
Shared Panel helpers for the smi-acquire mockups.

Builds Panel widgets from the pure-data :class:`smi_acquire.techniques.ParamSpec` /
``TechniqueSpec`` so both layout candidates (wizard, dashboard) stay DRY.  Importing this
module requires ``panel``; the headless core does not.
"""

from __future__ import annotations

import os
import sys

# Make the src/ package importable when run via `panel serve apps/...`.
_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

import panel as pn  # noqa: E402

from smi_acquire import samples, techniques, codegen  # noqa: E402

pn.extension("tabulator", "codeeditor", sizing_mode="stretch_width")

ACCENT = "#0072B5"

STARTER_ROWS = [
    {"name": "sample_01", "piezo_x": 55000.0, "piezo_y": 5000.0, "piezo_z": 7000.0,
     "piezo_th": None, "hexa_x": 10.0, "hexa_y": None, "hexa_z": None, "hexa_th": None,
     "incident_angles": "0.1 0.2", "md": ""},
    {"name": "sample_02", "piezo_x": 42000.0, "piezo_y": 5000.0, "piezo_z": 7000.0,
     "piezo_th": None, "hexa_x": 10.0, "hexa_y": None, "hexa_z": None, "hexa_th": None,
     "incident_angles": "0.1 0.2", "md": ""},
    {"name": "sample_03", "piezo_x": 25000.0, "piezo_y": 5000.0, "piezo_z": 7000.0,
     "piezo_th": None, "hexa_x": 10.0, "hexa_y": None, "hexa_z": None, "hexa_th": None,
     "incident_angles": "0.1 0.2", "md": ""},
]

_TABULATOR_COLS = samples.SAMPLE_FIELDS + ["md"]


def empty_frame():
    import pandas as pd
    return pd.DataFrame(STARTER_ROWS, columns=_TABULATOR_COLS)


def sample_table(value=None, height=260):
    """A spreadsheet-like, paste-friendly Tabulator editor for the sample bar."""
    import pandas as pd
    df = value if value is not None else empty_frame()
    return pn.widgets.Tabulator(
        pd.DataFrame(df, columns=_TABULATOR_COLS),
        height=height, layout="fit_data_stretch", show_index=False,
        widths={"name": 120, "md": 200},
        editors={c: {"type": "number"} if c in samples.NUMERIC_FIELDS else {"type": "input"}
                 for c in _TABULATOR_COLS},
    )


def table_to_samplelist(table):
    """Tabulator value (DataFrame) -> SampleList, tolerant of blanks."""
    recs = table.value.to_dict("records")
    return samples.records_to_samples(recs)


# ---------------------------------------------------------------------------
# Param widgets from a TechniqueSpec
# ---------------------------------------------------------------------------
def widget_for(p: techniques.ParamSpec):
    name = p.label
    if p.kind == "bool":
        return pn.widgets.Checkbox(name=name, value=bool(p.default))
    if p.kind == "int":
        return pn.widgets.IntInput(name=name, value=int(p.default))
    if p.kind == "float":
        return pn.widgets.FloatInput(name=name, value=float(p.default))
    if p.kind == "optfloat":
        val = "" if p.default in (None, "") else str(p.default)
        return pn.widgets.TextInput(name=name + " (blank=None)", value=val)
    if p.kind in ("choice", "token") and p.choices:
        return pn.widgets.Select(name=name, value=p.default, options=list(p.choices))
    if p.kind in ("floats", "tuple"):
        text = ", ".join(str(x) for x in (p.default or []))
        return pn.widgets.TextInput(name=name, value=text)
    # str / token without choices
    return pn.widgets.TextInput(name=name, value=str(p.default))


def read_widget(p: techniques.ParamSpec, w):
    if p.kind == "optfloat":
        v = (w.value or "").strip()
        return None if v == "" else float(v)
    if p.kind in ("floats", "tuple"):
        return [float(x) for x in str(w.value).replace(";", " ").replace(",", " ").split()]
    return w.value


def param_form(spec: techniques.TechniqueSpec):
    """Return (column_of_widgets, read_values()) for a technique's parameters, grouped."""
    groups = {}
    widgets = {}
    for p in spec.params:
        w = widget_for(p)
        widgets[p.name] = (p, w)
        groups.setdefault(p.group, []).append(w)

    cards = []
    for group, ws in groups.items():
        cards.append(pn.Card(*ws, title=group, collapsed=(group == "Idioms")))
    col = pn.Column(*cards)

    def read_values():
        return {name: read_widget(p, w) for name, (p, w) in widgets.items()}

    return col, read_values


def technique_options():
    """[(label, letter), ...] for all A-O (bar + special), ordered."""
    opts = {}
    for letter in techniques.all_letters():
        spec = techniques.get(letter)
        if spec is not None:
            opts["{} - {}".format(letter, spec.title)] = letter
        else:
            sp = techniques.SPECIAL[letter]
            opts["{} - {}".format(letter, sp["title"])] = letter
    return opts


def technique_summary(letter):
    spec = techniques.get(letter)
    if spec is not None:
        needs = (" \n\n**Needs at runtime:** " + ", ".join(spec.needs)) if spec.needs else ""
        return "### {} - {}\n\n{}\n\n*Recommended when:* {}{}".format(
            spec.letter, spec.title, spec.summary, spec.recommend_when, needs)
    sp = techniques.SPECIAL[letter]
    return "### {} - {}\n\n{}\n\n*Recommended when:* {}".format(
        letter, sp["title"], sp["summary"], sp["recommend_when"])
