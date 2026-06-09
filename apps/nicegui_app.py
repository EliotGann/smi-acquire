"""
NiceGUI mockup -- LAYOUT CANDIDATE 4
====================================

A lighter-weight web alternative to Panel, to compare ergonomics.  NiceGUI gives you a very
compact, reactive Python-only API (Vue/Quasar under the hood) that is pleasant for form-heavy
tools like this one.  Same headless core, so this file is again pure UI.

Layout: a two-column reactive page with an `ui.stepper`-free, always-visible design --
inputs on the left (sample grid + technique + params), live script on the right.

NiceGUI is not in the smi-browser pixi env by default; add it with ``pixi add --pypi nicegui``
(or use the ``[feature.nicegui]`` env in pixi.toml) and run::

    python apps/nicegui_app.py

then open http://localhost:8080.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from smi_acquire import samples, techniques, guidance, codegen

try:
    from nicegui import ui
except Exception as exc:  # pragma: no cover - nicegui optional
    raise SystemExit(
        "nicegui not installed. Try `pixi add --pypi nicegui` then rerun. (" + str(exc) + ")")


COLS = samples.SAMPLE_FIELDS + ["md"]
rows = [
    {"name": "sample_01", "piezo_x": "55000", "piezo_y": "5000", "piezo_z": "7000",
     "incident_angles": "0.1 0.2", "md": ""},
    {"name": "sample_02", "piezo_x": "42000", "piezo_y": "5000", "piezo_z": "7000",
     "incident_angles": "0.1 0.2", "md": ""},
]

state = {"letter": "A", "readers": {}, "project": ""}


@ui.page("/")
def index():
    with ui.row().classes("w-full no-wrap"):
        # ---- LEFT: inputs --------------------------------------------------
        with ui.column().classes("w-1/2"):
            ui.label("SMI-SWAXS Acquire - NiceGUI").classes("text-h5")

            ui.label("Sample bar").classes("text-bold mt-2")
            grid = ui.aggrid({
                "columnDefs": [{"headerName": c, "field": c, "editable": True} for c in COLS],
                "rowData": rows,
                "defaultColDef": {"flex": 1, "minWidth": 80},
            }).classes("h-64")

            with ui.row():
                ui.button("+ Row", on_click=lambda: _add_row(grid))
                ui.button("Reload", on_click=lambda: grid.update())

            project = ui.input("Project name (md)").classes("w-full")
            project.on_value_change(lambda e: state.update(project=e.value) or _regen())

            ui.label("Goal filter").classes("text-bold mt-2")
            cv = {label: val for val, label in guidance.questions()[0]["options"]}
            cv_sel = ui.select({"": "(any)", **{v: k for k, v in cv.items()}},
                               value="", label="I'm varying...").classes("w-full")

            tech_opts = {}
            for letter in techniques.all_letters():
                spec = techniques.get(letter)
                title = spec.title if spec else techniques.SPECIAL[letter]["title"]
                tech_opts[letter] = "{} - {}".format(letter, title)
            tech_sel = ui.select(tech_opts, value="A", label="Technique").classes("w-full")

            tech_info = ui.markdown("")
            form_host = ui.column().classes("w-full")

            def _rank(_):
                if not cv_sel.value:
                    return
                recs = guidance.recommend({"control_variable": cv_sel.value})
                if recs:
                    tech_sel.value = recs[0]["letter"]

            cv_sel.on_value_change(_rank)

            def _build_form(_=None):
                letter = tech_sel.value
                state["letter"] = letter
                spec = techniques.get(letter)
                form_host.clear()
                state["readers"] = {}
                tech_info.content = "*{}*".format(
                    spec.summary if spec else techniques.SPECIAL[letter]["summary"])
                if spec is None:
                    with form_host:
                        ui.label("Run/loop archetype - starter template emitted at right.")
                    _regen()
                    return
                with form_host:
                    for p in spec.params:
                        state["readers"][p.name] = _field(p)
                _regen()

            tech_sel.on_value_change(_build_form)

        # ---- RIGHT: script -------------------------------------------------
        with ui.column().classes("w-1/2"):
            ui.label("Generated script").classes("text-bold")
            code = ui.code("", language="python").classes("w-full h-96")
            status = ui.label("")

            def _regen():
                try:
                    recs = grid.options["rowData"]
                    bar = samples.records_to_samples(recs)
                    values = {n: r() for n, r in state["readers"].items()}
                    pmd = ({"project_name": state["project"]}
                           if state["project"].strip() else None)
                    code.content = codegen.generate_script(
                        bar, state["letter"], values, project_md=pmd)
                    status.text = "OK - {} sample(s)".format(len(bar))
                except Exception as exc:
                    code.content = "# ERROR: {}".format(exc)
                    status.text = "Error: {}".format(exc)

            # expose to closures above
            globals()["_regen"] = _regen

        def _field(p: techniques.ParamSpec):
            if p.kind == "bool":
                w = ui.checkbox(p.label, value=bool(p.default))
                w.on_value_change(lambda e: _regen())
                return lambda: w.value
            if p.kind in ("choice", "token") and p.choices:
                w = ui.select(list(p.choices), value=p.default, label=p.label)
                w.on_value_change(lambda e: _regen())
                return lambda: w.value
            default = (", ".join(str(x) for x in p.default)
                       if isinstance(p.default, (list, tuple))
                       else ("" if p.default is None else str(p.default)))
            w = ui.input(p.label, value=default).classes("w-full")
            w.on_value_change(lambda e: _regen())

            def reader():
                txt = (w.value or "").strip()
                if p.kind == "optfloat":
                    return None if txt == "" else float(txt)
                if p.kind in ("floats", "tuple"):
                    return [float(x) for x in txt.replace(";", " ").replace(",", " ").split()]
                if p.kind == "int":
                    return int(float(txt))
                if p.kind == "float":
                    return float(txt)
                return txt
            return reader

        def _add_row(g):
            n = len(g.options["rowData"]) + 1
            g.options["rowData"].append({"name": "sample_%02d" % n})
            g.update()
            globals()["_regen"]()

        _build_form()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="SMI-SWAXS Acquire", port=8080, reload=False)
