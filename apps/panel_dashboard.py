"""
Panel layout candidate 2 -- SINGLE-PAGE DASHBOARD
=================================================

Everything on one screen, live-updating.  Best for power users / staff who know what they
want and iterate fast.  Contrast with the wizard (panel_wizard.py): no steps, no hand-holding,
maximal density.

Run::

    panel serve apps/panel_dashboard.py --show

Three columns:

  LEFT   -- the sample bar (Tabulator) + add/remove/duplicate + CSV + project md.
  CENTER -- technique picker (with a compact guidance "filter") + grouped parameter form.
  RIGHT  -- the live-generated script (regenerates on any change) + a "queueserver item"
            preview (the future submit path) + a download button.
"""

from __future__ import annotations

import panel as pn

import _panel_common as C
from smi_acquire import codegen, techniques, guidance

state = {"reader": lambda: {}}

# --- LEFT: samples ----------------------------------------------------------
table = C.sample_table(height=420)
add_btn = pn.widgets.Button(name="+ Row", width=80)
del_btn = pn.widgets.Button(name="- Row", width=80)
dup_btn = pn.widgets.Button(name="Duplicate", width=90)
csv_input = pn.widgets.FileInput(accept=".csv")
project_name = pn.widgets.TextInput(name="Project name (md)", placeholder="311234_Demo")


def _add(_):
    import pandas as pd
    n = len(table.value) + 1
    row = {c: ("sample_%02d" % n if c == "name" else
               ("" if c in ("md", "incident_angles") else None))
           for c in C._TABULATOR_COLS}
    table.value = pd.concat([table.value, pd.DataFrame([row])], ignore_index=True)


def _del(_):
    if len(table.value) > 1:
        table.value = table.value.iloc[:-1].reset_index(drop=True)


def _dup(_):
    import pandas as pd
    if len(table.value):
        last = table.value.iloc[[-1]].copy()
        last["name"] = str(last["name"].iloc[0]) + "_copy"
        table.value = pd.concat([table.value, last], ignore_index=True)


def _load_csv(_):
    if not csv_input.value:
        return
    import io
    import pandas as pd
    df = pd.read_csv(io.BytesIO(csv_input.value))
    for col in C._TABULATOR_COLS:
        if col not in df.columns:
            df[col] = "" if col in ("name", "md", "incident_angles") else None
    table.value = df[C._TABULATOR_COLS]


add_btn.on_click(_add)
del_btn.on_click(_del)
dup_btn.on_click(_dup)
csv_input.param.watch(lambda e: _load_csv(e), "value")

left = pn.Card(
    table,
    pn.Row(add_btn, del_btn, dup_btn),
    pn.pane.Markdown("**Load CSV**"), csv_input,
    project_name,
    title="Sample bar", collapsed=False,
)

# --- CENTER: guidance filter + technique + params ---------------------------
cv_opts = {label: val for val, label in guidance.questions()[0]["options"]}
cv_filter = pn.widgets.Select(name="I'm varying...", options={"(any)": ""} | cv_opts,
                              value="")
technique_select = pn.widgets.Select(name="Technique", options=C.technique_options(),
                                      value="A")
technique_info = pn.pane.Markdown(C.technique_summary("A"))
param_holder = pn.Column()


def _apply_filter(*_):
    if not cv_filter.value:
        technique_select.options = C.technique_options()
        return
    recs = guidance.recommend({"control_variable": cv_filter.value})
    ranked = [r["letter"] for r in recs]
    opts = C.technique_options()
    # reorder: recommended first
    ordered = {}
    for label, letter in sorted(opts.items(),
                                key=lambda kv: (ranked.index(kv[1])
                                                if kv[1] in ranked else 99, kv[0])):
        ordered[label] = letter
    technique_select.options = ordered
    if ranked:
        technique_select.value = ranked[0]


def _build_params(letter):
    spec = techniques.get(letter)
    param_holder.clear()
    if spec is None:
        param_holder.append(pn.pane.Markdown(
            "_Run/loop-based archetype -- a starter template is emitted on the right._"))
        state["reader"] = lambda: {}
        return
    col, reader = C.param_form(spec)
    state["reader"] = reader
    param_holder.append(col)


def _on_technique(*_):
    technique_info.object = C.technique_summary(technique_select.value)
    _build_params(technique_select.value)
    _regen()


cv_filter.param.watch(_apply_filter, "value")
technique_select.param.watch(_on_technique, "value")

center = pn.Card(cv_filter, technique_select, technique_info,
                 pn.layout.Divider(), param_holder,
                 title="Technique & parameters")

# --- RIGHT: live script + qserver preview -----------------------------------
code = pn.widgets.CodeEditor(language="python", theme="monokai", height=380, readonly=True)
qs_preview = pn.widgets.CodeEditor(language="json", theme="monokai", height=160,
                                   readonly=True)
download = pn.widgets.FileDownload(filename="smi_acquire_plan.py", button_type="primary",
                                   label="Download .py", callback=lambda: _script_text())
status = pn.pane.Markdown("")


def _current_inputs():
    bar = C.table_to_samplelist(table)
    pmd = {"project_name": project_name.value} if project_name.value.strip() else None
    return bar, technique_select.value, state["reader"](), pmd


def _script_text():
    import io
    bar, letter, values, pmd = _current_inputs()
    return io.StringIO(codegen.generate_script(bar, letter, values, project_md=pmd))


def _regen(*_):
    try:
        bar, letter, values, pmd = _current_inputs()
        code.value = codegen.generate_script(bar, letter, values, project_md=pmd)
        if techniques.get(letter) is not None:
            import json
            qs_preview.value = json.dumps(
                codegen.to_queueserver_item(bar, letter, values, project_md=pmd), indent=2)
        else:
            qs_preview.value = "// (no bar entry point for this archetype)"
        status.object = "OK - {} sample(s), technique {}".format(len(bar), letter)
    except Exception as exc:
        code.value = "# ERROR: {}".format(exc)
        status.object = "**Error:** {}".format(exc)


table.param.watch(_regen, "value")
project_name.param.watch(_regen, "value")

right = pn.Card(status, code,
                pn.pane.Markdown("**Future: queueserver item** (submit path preview)"),
                qs_preview, download,
                title="Generated script")

# Regenerate when any parameter widget changes: re-wire after each rebuild.
_orig_build = _build_params


def _build_params_wired(letter):
    _orig_build(letter)
    for card in param_holder.objects:
        if isinstance(card, pn.Column):
            for sub in card.objects:
                for w in getattr(sub, "objects", []):
                    if hasattr(w, "param") and hasattr(w, "value"):
                        w.param.watch(_regen, "value")


_build_params = _build_params_wired

template = pn.template.FastListTemplate(
    title="SMI-SWAXS Acquire - Dashboard",
    accent_base_color=C.ACCENT, header_background=C.ACCENT,
    main=[pn.Row(pn.Column(left, width=420),
                 pn.Column(center, width=420),
                 pn.Column(right, sizing_mode="stretch_width"))],
)
_build_params("A")
_regen()
template.servable()
