"""
Panel layout candidate 1 -- GUIDED WIZARD
=========================================

A linear, low-friction "answer a few questions -> we pick the tool -> fill params -> copy the
script" flow.  Best for occasional / new users who do not know the A--O taxonomy.

Run::

    panel serve apps/panel_wizard.py --show

Five steps, one visible at a time, with a persistent progress rail on the left:

  1. Samples   -- paste / edit the bar (Tabulator), or load a CSV.
  2. Goal      -- the guidance questions; we rank techniques and pre-select the top one.
  3. Technique -- confirm / override the technique (full A-O list + summary).
  4. Parameters-- the technique's typed form, grouped into cards.
  5. Script    -- the generated, copyable script (+ live re-generate).
"""

from __future__ import annotations

import panel as pn

import _panel_common as C
from smi_acquire import guidance, codegen, techniques

STEPS = ["1. Samples", "2. Goal", "3. Technique", "4. Parameters", "5. Script"]
state = {"letter": "A", "values": {}}

# ---- step 1: samples -------------------------------------------------------
table = C.sample_table()
csv_input = pn.widgets.FileInput(accept=".csv", name="Load CSV")
project_name = pn.widgets.TextInput(name="Project name (md)", placeholder="e.g. 311234_Demo")


def _load_csv(event):
    if not csv_input.value:
        return
    import io
    import pandas as pd
    df = pd.read_csv(io.BytesIO(csv_input.value))
    for col in C._TABULATOR_COLS:
        if col not in df.columns:
            df[col] = "" if col in ("name", "md", "incident_angles") else None
    table.value = df[C._TABULATOR_COLS]


csv_input.param.watch(_load_csv, "value")

step1 = pn.Column(
    pn.pane.Markdown("## Build your sample bar\n"
                     "Paste rows, edit cells, or load a CSV. Only the axes you set are used; "
                     "blanks mean *don't move that axis*."),
    table,
    pn.Row(csv_input, project_name),
)

# ---- step 2: goal / guidance ----------------------------------------------
q_widgets = {}
q_col = pn.Column(pn.pane.Markdown("## What are you trying to do?"))
for q in guidance.questions():
    opts = {label: val for val, label in q["options"]}
    if q.get("multi"):
        w = pn.widgets.CheckBoxGroup(name=q["prompt"], options=opts, value=[])
    else:
        w = pn.widgets.RadioBoxGroup(name=q["prompt"], options=opts,
                                     value=list(opts.values())[0])
    q_widgets[q["key"]] = w
    q_col.append(pn.pane.Markdown("**" + q["prompt"] + "**"))
    q_col.append(w)
keywords = pn.widgets.TextInput(name="Keywords (optional)", placeholder="e.g. resonant edge")
q_col.append(keywords)
recommendation = pn.pane.Markdown("")
q_col.append(recommendation)


def _answers():
    return {k: w.value for k, w in q_widgets.items()}


def _recommend(*_):
    recs = guidance.recommend(_answers(), keywords=keywords.value)
    if not recs:
        recommendation.object = "_Make a selection above to get a recommendation._"
        return
    top = recs[0]
    state["letter"] = top["letter"]
    technique_select.value = top["letter"]
    lines = ["### Suggested: **{} - {}**".format(top["letter"], top["title"]),
             "Because: " + "; ".join(top["reasons"]) + "."]
    if len(recs) > 1:
        lines.append("\n_Alternatives:_ " + ", ".join(
            "{} ({})".format(r["letter"], r["title"]) for r in recs[1:4]))
    recommendation.object = "\n\n".join(lines)


for w in list(q_widgets.values()) + [keywords]:
    w.param.watch(_recommend, "value")

# ---- step 3: technique confirm --------------------------------------------
technique_select = pn.widgets.Select(name="Technique", options=C.technique_options(),
                                     value="A")
technique_info = pn.pane.Markdown(C.technique_summary("A"))
step3 = pn.Column(pn.pane.Markdown("## Confirm the technique"),
                  technique_select, technique_info)

# ---- step 4: parameters ----------------------------------------------------
param_holder = pn.Column()
read_values = {"fn": lambda: {}}


def _build_params(letter):
    spec = techniques.get(letter)
    param_holder.clear()
    if spec is None:
        param_holder.append(pn.pane.Markdown(
            "_This archetype is run/loop-based; the script step emits a starter template._"))
        read_values["fn"] = lambda: {}
        return
    col, reader = C.param_form(spec)
    read_values["fn"] = reader
    param_holder.append(col)


step4 = pn.Column(pn.pane.Markdown("## Set parameters"), param_holder)


def _on_technique(*_):
    letter = technique_select.value
    state["letter"] = letter
    technique_info.object = C.technique_summary(letter)
    _build_params(letter)


technique_select.param.watch(_on_technique, "value")

# ---- step 5: script --------------------------------------------------------
code = pn.widgets.CodeEditor(language="python", theme="monokai", height=420,
                             readonly=True, sizing_mode="stretch_width")
gen_btn = pn.widgets.Button(name="Generate / refresh script", button_type="primary",
                            icon="code")


def _generate(*_):
    bar = C.table_to_samplelist(table)
    pmd = {"project_name": project_name.value} if project_name.value.strip() else None
    try:
        values = read_values["fn"]()
        code.value = codegen.generate_script(bar, state["letter"], values, project_md=pmd)
    except Exception as exc:  # surface validation errors inline
        code.value = "# ERROR: {}".format(exc)


gen_btn.on_click(_generate)
step5 = pn.Column(pn.pane.Markdown("## Copy & run\n"
                                   "Review, then paste into the beamline IPython session."),
                  gen_btn, code)

# ---- wizard shell ----------------------------------------------------------
PAGES = [step1, step2 := q_col, step3, step4, step5]
idx = {"i": 0}
body = pn.Column(PAGES[0])
rail = pn.Column(*[pn.pane.Markdown("**> " + s + "**" if i == 0 else s)
                   for i, s in enumerate(STEPS)], width=180)
back_btn = pn.widgets.Button(name="< Back", width=100)
next_btn = pn.widgets.Button(name="Next >", button_type="primary", width=100)


def _show(i):
    idx["i"] = i
    body.clear()
    body.append(PAGES[i])
    rail.clear()
    for j, s in enumerate(STEPS):
        rail.append(pn.pane.Markdown("**> " + s + "**" if i == j else s))
    back_btn.disabled = i == 0
    next_btn.disabled = i == len(PAGES) - 1
    if i == 1:
        _recommend()
    if i == 3:
        _build_params(state["letter"])
    if i == 4:
        _generate()


back_btn.on_click(lambda e: _show(max(0, idx["i"] - 1)))
next_btn.on_click(lambda e: _show(min(len(PAGES) - 1, idx["i"] + 1)))

template = pn.template.FastListTemplate(
    title="SMI-SWAXS Acquire - Wizard",
    accent_base_color=C.ACCENT, header_background=C.ACCENT,
    sidebar=[pn.pane.Markdown("### Steps"), rail,
             pn.pane.Markdown("---\n_Layout candidate 1: guided wizard._")],
    main=[pn.Column(body, pn.Row(back_btn, next_btn, align="end"))],
)
_build_params("A")
template.servable()
