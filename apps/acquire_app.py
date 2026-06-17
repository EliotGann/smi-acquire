"""
SMI-SWAXS Acquire — sample-centric acquisition builder
======================================================

The **sample list is the spine** (persistent sidebar). Two screens orbit it:

* **Align & Samples** (home) — the live on-axis microscope: click-to-move, fast line/grid/
  polygon **alignment** scans, and capture stage positions into samples ("★ new sample here" /
  "assign → selected"). Positioned, visible samples appear as markers on the image.
* **Plan** — build a scan **recipe** (directly, or seeded by a short interview), target a
  **sample-set**, see the generated ``smi_plans`` script + a dry-run, and save it as an
  Experiment on the project.

Hardware-free: the microscope talks to the bundled fake caproto IOC (``pixi run dev-ioc``);
validation runs against an in-process simulated beamline.

Run::

    pixi run dev-ioc      # terminal 1
    pixi run app          # terminal 2 → http://localhost:5098/acquire_app
"""

from __future__ import annotations

import io
import json

import pandas as pd
import panel as pn

from smi_acquire import interview, codegen, dryrun, registry
from smi_acquire.interview import axis_param_schema, default_axis, reorder_axes_by_speed
from smi_acquire.project import Project, Experiment, Target, Reference
from smi_acquire.store import AcquireStore
from smi_acquire.execute import LocalExecutor, QueueServerExecutor, InterlockedError
from smi_acquire.interlock import Interlock

pn.extension("tabulator", "codeeditor", sizing_mode="stretch_width", notifications=True)

ACCENT = "#0072B5"


# ---------------------------------------------------------------------------
# small parse/format helpers
# ---------------------------------------------------------------------------
def _parse_floatlist(text):
    if isinstance(text, (list, tuple)):
        return list(text)
    out = []
    for tok in str(text or "").replace(";", " ").replace(",", " ").split():
        try:
            f = float(tok)
            out.append(int(f) if f.is_integer() else f)
        except ValueError:
            pass
    return out


def _fmt_floatlist(vals):
    return " ".join(str(int(v) if isinstance(v, float) and v.is_integer() else v)
                    for v in (vals or []))


def _get(params, dotted):
    d = params
    for p in dotted.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _set(params, dotted, value):
    parts = dotted.split(".")
    for p in parts[:-1]:
        params = params.setdefault(p, {})
    params[parts[-1]] = value


def _toast(msg, kind="info"):
    try:
        getattr(pn.state.notifications, kind)(msg, duration=3000)
    except Exception:
        pass


# ===========================================================================
class AcquireApp:
    def __init__(self):
        self.store = AcquireStore.connect()    # live redis db=2 (auto-falls back offline)
        self.project = Project(name="")        # local: recipes + references only
        # Motion seam: jog directly via ophyd (interlock-gated); submit = copy-paste to the
        # beamline RunEngine.  The interlock reads the external RE-busy flag (db=3, read-only).
        self.interlock = Interlock.from_redis()
        self.executor = LocalExecutor(interlock=self.interlock)
        self.micro = None                 # MicroscopeUI (lazy: needs the IOC)
        self.current_exp: Experiment | None = None
        self._spine_ids: list[str] = []   # store sample ids, parallel to the spine df rows
        self._build()

    # -- microscope marker sync --------------------------------------------
    def sync_markers(self):
        """Push markers onto the image: positioned store samples (lime) + local references (yellow)."""
        if self.micro is None:
            return
        from smi_acquire.microscope.scripts import Bookmark as MBookmark
        inter = self.micro.interactive
        bms, mab = [], []
        for s in self.store.list_samples():
            xyz = self._sample_xyz(s)
            if xyz is None:
                continue
            x, y, z = xyz
            bms.append(MBookmark(name=s.name, x=x, y=y, z=z, is_reference=False))
            mab.append((x, y, z))
        for r in self.project.references:
            if not r.visible:
                continue
            x, y, z = (r.x or 0.0), (r.y or 0.0), (r.z or 0.0)
            bms.append(MBookmark(name=r.name, x=x, y=y, z=z, is_reference=True))
            mab.append((x, y, z))
        inter._bookmarks = bms            # noqa: SLF001 (intentional bridge into vendored mode)
        inter._motor_at_bookmark = mab    # noqa: SLF001
        try:
            inter.tick()
            inter.tick_table()
        except Exception:
            pass

    @staticmethod
    def _sample_xyz(sample):
        """The x/y/z (piezo, stage fallback) of a sample's runnable position, or None if unpositioned."""
        p = sample.runnable_position()
        x = p.piezo_x if p.piezo_x is not None else p.stage_x
        y = p.piezo_y if p.piezo_y is not None else p.stage_y
        z = p.piezo_z if p.piezo_z is not None else p.stage_z
        if x is None and y is None and z is None:
            return None
        return (x or 0.0, y or 0.0, z or 0.0)

    @staticmethod
    def _is_positioned(sample):
        """True if any piezo_*/stage_* axis of the sample's runnable position is set."""
        p = sample.runnable_position()
        return any(getattr(p, a) is not None for a in
                   ("piezo_x", "piezo_y", "piezo_z", "piezo_th",
                    "stage_x", "stage_y", "stage_z",
                    "stage_theta", "stage_chi", "stage_phi"))

    # -- live stage axes (microscope reading) ------------------------------
    def _stage_axes(self) -> dict:
        """A dict of the axes the microscope exposes today: ``{"x":.., "y":.., "z":..}``."""
        if self.micro is None:
            return {}
        s = self.micro.stage
        try:
            return {"x": round(float(s.x.position), 4),
                    "y": round(float(s.y.position), 4),
                    "z": round(float(s.z.position), 4)}
        except Exception:
            return {}

    # ==================================================================
    # THE SPINE (persistent sidebar) — samples FROM THE SHARED STORE
    # ==================================================================
    def _build_spine(self):
        self.spine = pn.widgets.Tabulator(
            value=self._spine_df(), show_index=False, selectable=1, height=320, theme="simple",
            widths={"name": 110, "holder": 90, "x": 60, "y": 60, "z": 52, "incidence": 80,
                    "active": 50, "md": 120},
            editors={"name": {"type": "input"}, "holder": {"type": "input"},
                     "incidence": {"type": "input"}, "md": {"type": "input"},
                     "x": None, "y": None, "z": None, "active": None},
        )
        self.spine.on_edit(self._on_spine_edit)

        add = pn.widgets.Button(name="+ blank sample", width=120)
        add.on_click(self._on_add_blank)
        rm = pn.widgets.Button(name="✕ remove selected", button_type="danger", width=140)
        rm.on_click(self._on_remove_selected)
        load = pn.widgets.Button(name="◆ load (set active)", button_type="primary", width=160)
        load.on_click(self._on_set_active)

        # Holders sub-panel (replaces the old "Sets").
        new_holder = pn.widgets.TextInput(placeholder="new holder name…", width=130)
        mk_holder = pn.widgets.Button(name="+ holder", width=80)
        mk_holder.on_click(lambda _e: self._make_holder(new_holder))
        self.move_holder = pn.widgets.Select(options=self._holder_options(), width=140)
        move_btn = pn.widgets.Button(name="→ move sel. to holder", width=170)
        move_btn.on_click(self._on_move_holder)

        imp = pn.widgets.FileInput(accept=".csv", name="import")
        imp.param.watch(self._on_import_csv, "value")
        self.export_csv = pn.widgets.FileDownload(
            callback=self._export_csv, filename="samples.csv", label="⬇ samples.csv", width=130)
        self.export_json = pn.widgets.FileDownload(
            callback=self._export_json, filename="project.json", label="⬇ project.json", width=130)
        load_json = pn.widgets.FileInput(accept=".json", name="load project")
        load_json.param.watch(self._on_load_json, "value")

        self.store_status = pn.pane.Markdown("")
        self._refresh_store_status()
        self.spine_count = pn.pane.Markdown("")
        self._refresh_spine_count()
        self.spine_panel = pn.Column(
            pn.pane.Markdown("### Sample list"),
            self.store_status,
            self.spine_count,
            self.spine,
            pn.Row(add, rm),
            pn.Row(load),
            pn.layout.Divider(),
            pn.pane.Markdown("**Holders**"),
            pn.Row(new_holder, mk_holder),
            pn.Row(self.move_holder, move_btn),
            pn.layout.Divider(),
            pn.pane.Markdown("**Import / export**"),
            pn.Row(imp),
            pn.Row(self.export_csv, self.export_json),
            pn.Row(pn.pane.Markdown("project recipes:"), load_json),
        )

    def _spine_df(self):
        active = self.store.active_sample()
        active_id = active.id if active is not None else None
        self._spine_ids = []
        rows = []
        for s in self.store.list_samples():
            self._spine_ids.append(s.id)
            holder = self.store.holder_by_id(s.holder_id) if s.holder_id else None
            p = s.nominal
            x = p.piezo_x if p.piezo_x is not None else p.stage_x
            y = p.piezo_y if p.piezo_y is not None else p.stage_y
            z = p.piezo_z if p.piezo_z is not None else p.stage_z
            angles = s.incident_angles or p.incident_angles
            rows.append({
                "name": s.name,
                "holder": holder.name if holder is not None else "",
                "x": x, "y": y, "z": z,
                "incidence": _fmt_floatlist(angles),
                "active": "◆" if s.id == active_id else "",
                "md": json.dumps(s.md) if s.md else "",
            })
        return pd.DataFrame(
            rows, columns=["name", "holder", "x", "y", "z", "incidence", "active", "md"])

    def refresh_spine(self):
        self.spine.value = self._spine_df()
        self.move_holder.options = self._holder_options()
        if hasattr(self, "capture_holder"):
            self.capture_holder.options = self._holder_options()
        self._refresh_store_status()
        self._refresh_spine_count()
        if hasattr(self, "target_select"):
            self.target_select.options = self._target_options()
        self.sync_markers()

    def _refresh_store_status(self):
        if self.store.live:
            self.store_status.object = ("<span style='color:#2e7d32'>● live: {}</span>"
                                        .format(self.store.location))
        else:
            self.store_status.object = ("<span style='color:#b26a00'>○ {}</span>"
                                        .format(self.store.location))

    def _refresh_spine_count(self):
        samples = self.store.list_samples()
        n = len(samples)
        pos = sum(1 for s in samples if self._is_positioned(s))
        self.spine_count.object = ("**{}** samples · {} positioned · {} holders · {} experiments"
                                   .format(n, pos, len(self.store.list_holders()),
                                           len(self.project.experiments)))

    def _holder_options(self):
        return {"(pick holder)": None,
                **{h.name: h.id for h in self.store.list_holders()}}

    def _selected_sample(self):
        sel = list(self.spine.selection or [])
        if not sel or sel[0] >= len(self._spine_ids):
            return None
        return self.store.sample_by_id(self._spine_ids[sel[0]])

    def _on_spine_edit(self, event):
        i = int(getattr(event, "row", -1))
        col = getattr(event, "column", None)
        val = getattr(event, "value", None)
        if not (0 <= i < len(self._spine_ids)):
            return
        s = self.store.sample_by_id(self._spine_ids[i])
        if s is None:
            return
        if col == "name":
            s.name = str(val).strip() or s.name
            self.store.update_sample(s)
        elif col == "holder":
            name = str(val or "").strip()
            if name:
                holder = self.store.ensure_holder(name)
                self.store.set_sample_holder(s.id, holder.id)
        elif col == "incidence":
            angles = _parse_floatlist(val)
            s.incident_angles = angles
            s.nominal.incident_angles = list(angles)
            self.store.update_sample(s)
        elif col == "md":
            try:
                s.md = json.loads(val) if str(val).strip() else {}
                self.store.update_sample(s)
            except Exception:
                _toast("metadata must be JSON", "warning")
        self.refresh_spine()

    def _on_add_blank(self, _e):
        self.store.add_sample(self._next_name())
        self.refresh_spine()

    def _on_remove_selected(self, _e):
        s = self._selected_sample()
        if s is not None:
            self.store.delete_sample(s.id)
            self.spine.selection = []
            self.refresh_spine()

    def _on_set_active(self, _e):
        s = self._selected_sample()
        if s is None:
            _toast("select a sample row in the sidebar first", "warning")
            return
        self.store.set_active_sample(s.id)
        self.refresh_spine()
        _toast("active sample set to intent — load '{}' from beamline session".format(s.name))

    def _next_name(self):
        existing = {s.name for s in self.store.list_samples()}
        i = 1
        while "sample{}".format(i) in existing:
            i += 1
        return "sample{}".format(i)

    def _make_holder(self, name_input):
        name = (name_input.value or "").strip()
        if name:
            self.store.ensure_holder(name)
            name_input.value = ""
            self.refresh_spine()

    def _on_move_holder(self, _e):
        s = self._selected_sample()
        hid = self.move_holder.value
        if s is not None and hid:
            self.store.set_sample_holder(s.id, hid)
            self.refresh_spine()

    def _on_import_csv(self, _e):
        if not self.spine.disabled and getattr(_e, "new", None):
            try:
                df = pd.read_csv(io.BytesIO(_e.new))
                n = self._import_samples_df(df)
                self.refresh_spine()
                _toast("imported {} samples".format(n))
            except Exception as exc:
                _toast("import failed: {}".format(exc), "error")

    def _import_samples_df(self, df):
        """Create store samples from a (tolerant) CSV. Coords -> nominal via position_from_axes."""
        coord_cols = {"x", "y", "z", "piezo_x", "piezo_y", "piezo_z", "piezo_th",
                      "stage_x", "stage_y", "stage_z", "stage_theta", "stage_chi", "stage_phi",
                      "nominal_piezo_x", "nominal_piezo_y", "nominal_piezo_z", "nominal_piezo_th",
                      "nominal_stage_x", "nominal_stage_y", "nominal_stage_z",
                      "nominal_stage_theta", "nominal_stage_chi", "nominal_stage_phi"}
        cols = list(df.columns)
        n = 0
        for _, r in df.iterrows():
            name = str(r["name"]).strip() if "name" in cols and pd.notna(r.get("name")) else None
            if not name:
                name = self._next_name()
            holder_id = None
            if "holder" in cols and pd.notna(r.get("holder")):
                holder_id = self.store.ensure_holder(str(r["holder"]).strip()).id
            axes = {}
            for c in coord_cols:
                if c in cols and pd.notna(r.get(c)):
                    key = c[len("nominal_"):] if c.startswith("nominal_") else c
                    try:
                        axes[key] = float(r[c])
                    except (TypeError, ValueError):
                        pass
            nominal = AcquireStore.position_from_axes(axes) if axes else None
            angles = _parse_floatlist(r["incident_angles"]) if (
                "incident_angles" in cols and pd.notna(r.get("incident_angles"))) else []
            md = {}
            for c in cols:
                if c.startswith("md.") and pd.notna(r.get(c)):
                    md[c[len("md."):]] = r[c]
            self.store.add_sample(name, holder_id=holder_id, nominal=nominal,
                                  incident_angles=angles, md=md)
            n += 1
        return n

    def _export_csv(self):
        samples_rows, _scans = self.store.store.export_tables()
        return io.BytesIO(pd.DataFrame(samples_rows).to_csv(index=False).encode())

    def _export_json(self):
        return io.BytesIO(json.dumps(self.project.to_dict(), indent=2).encode())

    def _on_load_json(self, _e):
        if getattr(_e, "new", None):
            try:
                self.project = Project.from_dict(json.loads(_e.new.decode()))
                self.current_exp = None
                self.refresh_spine()
                self._refresh_experiment_list()
                _toast("loaded project recipes ({} experiments)".format(
                    len(self.project.experiments)))
            except Exception as exc:
                _toast("load failed: {}".format(exc), "error")

    # ==================================================================
    # HOME — Align & Samples
    # ==================================================================
    def _build_home(self):
        self.micro_box = pn.Column(pn.pane.Markdown(
            "_microscope starts when you open this tab…_"))

        # RunEngine-busy interlock banner (hidden unless a scan is running on the beamline RE).
        self.interlock_banner = pn.pane.Alert("", alert_type="danger", visible=False)

        self.pos_readout = pn.pane.Markdown("position: —")
        # css class "bookmark-name" lets the in-image 'b' shortcut focus this field.
        self.capture_name = pn.widgets.TextInput(
            name="name", placeholder="sample name", width=160,
            css_classes=["bookmark-name"])
        self.capture_holder = pn.widgets.Select(
            name="onto holder", options=self._holder_options(), width=160)
        new_btn = pn.widgets.Button(name="★ new sample here", button_type="primary", width=160)
        new_btn.on_click(self._on_new_here)
        assign_btn = pn.widgets.Button(name="assign → selected", width=160)
        assign_btn.on_click(self._on_assign_here)
        ref_btn = pn.widgets.Button(name="+ reference here", width=160)
        ref_btn.on_click(self._on_ref_here)
        sync_btn = pn.widgets.Button(name="↻ markers from samples", width=180)
        sync_btn.on_click(lambda _e: self.sync_markers())

        # Capture-position controls. These are folded into the microscope's **Move** tab
        # (next to the bookmark list) once the microscope is built — see _ensure_microscope.
        self.capture_controls = pn.Column(
            pn.pane.Markdown("### Capture position\nMove with the image, then:"),
            self.interlock_banner,
            self.pos_readout,
            self.capture_name,
            self.capture_holder,
            pn.Row(new_btn, assign_btn),
            pn.Row(ref_btn, sync_btn),
            pn.pane.Markdown(
                "<span style='color:#777;font-size:12px'>Positioned samples show as "
                "lime markers; visible references as yellow. Use the **Scan** tabs for line/grid/"
                "polygon alignment.</span>"),
            sizing_mode="stretch_width",
        )
        self.home = self.micro_box

    def _refresh_interlock(self):
        """Poll the external RE-busy flag and show/hide the lockout banner (uncached)."""
        try:
            banner = self.interlock.banner()
        except Exception:
            banner = ""
        if banner:
            self.interlock_banner.object = banner
            self.interlock_banner.visible = True
        else:
            self.interlock_banner.visible = False

    def _ensure_microscope(self):
        if self.micro is not None:
            return
        try:
            from smi_acquire.microscope.builder import build_microscope
            ui = build_microscope(executor=self.executor)
            self.micro = ui
            # Fold the capture-position controls into the microscope's Move tab (combined
            # with the bookmark list — they were redundant as a separate panel).
            ui.capture_slot.append(self.capture_controls)
            ui.attach_periodic_callbacks()
            pn.state.add_periodic_callback(self._refresh_pos, period=500)
            # Poll the RE-busy interlock at ~1.5 Hz (uncached; the flag has a 30s TTL).
            pn.state.add_periodic_callback(self._refresh_interlock, period=650)
            self.micro_box.clear()
            self.micro_box.append(ui.layout)
            self.sync_markers()
        except Exception as exc:
            self.micro_box.clear()
            self.micro_box.append(pn.pane.Alert(
                "Microscope unavailable: {}\n\nStart the fake IOC: `pixi run dev-ioc`, then "
                "reload.".format(exc), alert_type="warning"))

    def _refresh_pos(self):
        ax = self._stage_axes()
        if ax:
            self.pos_readout.object = "position: **x {} · y {} · z {}**".format(
                ax.get("x"), ax.get("y"), ax.get("z"))

    def _on_new_here(self, _e):
        name = (self.capture_name.value or "").strip() or self._next_name()
        holder_id = self.capture_holder.value if hasattr(self, "capture_holder") else None
        self.store.add_sample(name, holder_id=holder_id,
                              nominal=AcquireStore.position_from_axes(self._stage_axes()))
        self.capture_name.value = ""
        self.refresh_spine()
        _toast("added sample '{}'".format(name))

    def _on_assign_here(self, _e):
        s = self._selected_sample()
        if s is None:
            _toast("select a sample row in the sidebar first", "warning")
            return
        self.store.assign_nominal(s.id, AcquireStore.position_from_axes(self._stage_axes()))
        self.refresh_spine()
        _toast("positioned '{}'".format(s.name))

    def _on_ref_here(self, _e):
        ax = self._stage_axes()
        name = (self.capture_name.value or "").strip() or "ref{}".format(
            len(self.project.references) + 1)
        self.project.references.append(
            Reference(name=name, x=ax.get("x"), y=ax.get("y"), z=ax.get("z")))
        self.capture_name.value = ""
        self.refresh_spine()
        _toast("added reference '{}'".format(name))

    # ==================================================================
    # PLAN — Experiment
    # ==================================================================
    def _build_plan(self):
        self.exp_select = pn.widgets.Select(name="Experiment", options=self._exp_options(), width=240)
        self.exp_select.param.watch(self._on_pick_exp, "value")
        new_exp = pn.widgets.Button(name="+ new experiment", width=150)
        new_exp.on_click(self._on_new_exp)
        del_exp = pn.widgets.Button(name="✕ delete", button_type="danger", width=90)
        del_exp.on_click(self._on_del_exp)

        self.editor_box = pn.Column()
        self.code = pn.widgets.CodeEditor(language="python", theme="monokai", height=380,
                                          readonly=True, sizing_mode="stretch_width")
        validate = pn.widgets.Button(name="Validate (dry-run)", button_type="primary", width=180)
        validate.on_click(lambda _e: self._validate())
        self.report = pn.pane.Markdown("")

        # Submission seam: copy the RE(...) script to paste into the beamline session (now), or
        # submit to the queueserver (stub — qserver is not in use yet, so this is disabled).
        self.copy_re_btn = pn.widgets.Button(
            name="⧉ Copy RE command", button_type="success", width=200)
        self.copy_re_btn.on_click(lambda _e: self._submit_copy())
        self.submit_q_btn = pn.widgets.Button(
            name="⇪ Submit to queue (qserver — N/A)", button_type="default", width=260,
            disabled=True)
        self.submit_q_btn.on_click(lambda _e: self._submit_queue())
        self.submit_status = pn.pane.Markdown("")

        self.plan = pn.Column(
            pn.pane.Markdown("## Build a scan recipe → target a holder"),
            pn.Row(self.exp_select, new_exp, del_exp),
            self.editor_box,
            pn.layout.Divider(),
            pn.Row(validate, self.copy_re_btn, self.submit_q_btn),
            self.submit_status,
            self.report,
            self.code,
        )
        if self.project.experiments:
            self.current_exp = self.project.experiments[0]
        self._render_editor()

    def _exp_options(self):
        opts = {"(no experiment)": None}
        opts.update({e.name: e.id for e in self.project.experiments})
        return opts

    def _target_options(self):
        opts = {"(all samples)": "all"}
        opts.update({"holder: " + h.name: "holder:" + h.id
                     for h in self.store.list_holders()})
        return opts

    def _refresh_experiment_list(self):
        self.exp_select.options = self._exp_options()
        self.exp_select.value = self.current_exp.id if self.current_exp else None

    def _on_pick_exp(self, _e):
        eid = self.exp_select.value
        self.current_exp = next((e for e in self.project.experiments if e.id == eid), None)
        self._render_editor()

    def _on_new_exp(self, _e):
        e = Experiment(name="experiment {}".format(len(self.project.experiments) + 1))
        self.project.experiments.append(e)
        self.current_exp = e
        self._refresh_experiment_list()
        self._render_editor()
        self.refresh_spine()

    def _on_del_exp(self, _e):
        if self.current_exp in self.project.experiments:
            self.project.experiments.remove(self.current_exp)
            self.current_exp = self.project.experiments[0] if self.project.experiments else None
            self._refresh_experiment_list()
            self._render_editor()
            self.refresh_spine()

    def _render_editor(self):
        self.editor_box.clear()
        e = self.current_exp
        if e is None:
            self.editor_box.append(pn.pane.Markdown(
                "_No experiment selected. Create one, or seed it from the interview below._"))
            self.editor_box.append(self._interview_card(target=None))
            self.code.value = ""
            return

        name = pn.widgets.TextInput(name="Experiment name", value=e.name)
        scan = pn.widgets.TextInput(name="Scan name (run label)", value=e.scan_name)
        self.target_select = pn.widgets.Select(name="Target", options=self._target_options(),
                                               value=self._target_value(e.target))
        geo = pn.widgets.Select(name="Geometry", options=["transmission", "reflection"],
                                value=e.apparatus.geometry)
        exp_t = pn.widgets.FloatInput(name="Exposure (s)", value=e.beam.exposure_s, step=0.1)

        def _apply(_ev):
            e.name = name.value
            e.scan_name = scan.value
            e.target = self._parse_target(self.target_select.value)
            e.apparatus.geometry = geo.value
            e.beam.exposure_s = exp_t.value
            self._refresh_experiment_list()
            self._render_script()
            self.refresh_spine()
        for w in (name, scan, self.target_select, geo, exp_t):
            w.param.watch(_apply, "value")

        self.editor_box.extend([
            pn.Row(name, scan),
            pn.Row(self.target_select, geo, exp_t),
            self._beam_card(e),
            self._apparatus_card(e),
            self._axes_card(e),
            self._manual_card(e),
            self._interview_card(target=e),
        ])
        self._render_script()

    def _target_value(self, t: Target):
        if t.kind == "holder" and t.holder_id:
            return "holder:" + t.holder_id
        return "all"

    def _parse_target(self, val):
        if val and val.startswith("holder:"):
            return Target(kind="holder", holder_id=val[len("holder:"):])
        return Target(kind="all")

    # ---- concern cards (operate on the current Experiment) ------------
    def _beam_card(self, e):
        dets = pn.widgets.MultiChoice(name="Detectors", options=registry.detector_names(),
                                      value=list(e.beam.detectors))
        arc = pn.widgets.Checkbox(name="arc-aware (saxs_waxs_dets)", value=e.beam.arc_aware)
        reads = pn.widgets.MultiChoice(name="Record per event", options=registry.read_names(),
                                       value=list(e.beam.reads))

        def _apply(_ev):
            e.beam.detectors = list(dets.value)
            e.beam.arc_aware = arc.value
            e.beam.reads = list(reads.value)
            self._render_script()
        for w in (dets, arc, reads):
            w.param.watch(_apply, "value")
        return _card("Beam / q-range", dets, arc, reads)

    def _apparatus_card(self, e):
        ap = e.apparatus
        heater = pn.widgets.Select(name="Heater", options={"(none)": None, **{
            registry.HEATERS[k]: k for k in registry.HEATERS}}, value=ap.heater)
        align = pn.widgets.Select(name="Alignment routine", options={"(none)": None, **{
            r: r for r in registry.ALIGNMENT_ROUTINES}}, value=ap.align_routine)
        angle = pn.widgets.FloatInput(name="Align angle", value=ap.align_angle, step=0.05)
        atts = pn.widgets.MultiChoice(name="Attenuators in", options=registry.ATTENUATORS,
                                      value=list(ap.attenuators_in))

        def _apply(_ev):
            ap.heater, ap.align_routine = heater.value, align.value
            ap.align_angle, ap.attenuators_in = angle.value, list(atts.value)
            self._render_script()
        for w in (heater, align, angle, atts):
            w.param.watch(_apply, "value")
        return _card("Apparatus / geometry (→ setup)", pn.Row(heater, align), pn.Row(angle, atts))

    def _axes_card(self, e):
        status = pn.pane.Markdown("")
        inner = pn.Column()

        def _render_axes():
            inner.clear()
            if not e.axes:
                inner.append(pn.pane.Markdown("_no axes — a single point per sample._"))
            for i, ax in enumerate(e.axes):
                inner.append(self._axis_row(e, i, ax, _render_axes, status))
            _update_status()

        def _update_status():
            from smi_acquire.spec import ExperimentSpec
            sp = ExperimentSpec(axes=e.axes)
            warns = sp.order_warnings()
            msg = "**Nesting:** {}  →  **{:,} events/sample**".format(
                sp.summary(), sp.events_per_sample())
            if warns:
                msg += "\n\n" + "\n".join("⚠️ {}".format(w) for w in warns)
            status.object = msg
            self._render_script()

        add = pn.widgets.Select(options={"+ add axis…": None, **{
            k.label: k.type for k in registry.AXIS_KINDS}}, width=240)

        def _on_add(ev):
            if ev.new:
                e.axes.append(default_axis(ev.new))
                add.value = None
                _render_axes()
        add.param.watch(_on_add, "value")
        sort_btn = pn.widgets.Button(name="↓ sort slow-outermost", width=180)
        sort_btn.on_click(lambda _e: (setattr(e, "axes", reorder_axes_by_speed(e.axes)),
                                      _render_axes()))
        _render_axes()
        return _card("Scan axes (outermost → innermost)", status, inner, pn.Row(add, sort_btn))

    def _axis_row(self, e, i, ax, rerender, status):
        kind = registry.AXIS_KIND_BY_TYPE.get(ax.type)
        speed = {0: "fast", 1: "med", 2: "slow"}[ax.speed]
        header = pn.pane.Markdown("**{}. {}** · _{}_ · {} pts".format(
            i + 1, kind.label if kind else ax.type, speed, ax.n_points()))
        up = pn.widgets.Button(name="▲", width=36)
        down = pn.widgets.Button(name="▼", width=36)
        rm = pn.widgets.Button(name="✕", button_type="danger", width=36)

        def _move(d):
            j = i + d
            if 0 <= j < len(e.axes):
                e.axes[i], e.axes[j] = e.axes[j], e.axes[i]
                rerender()
        up.on_click(lambda _e: _move(-1))
        down.on_click(lambda _e: _move(+1))
        rm.on_click(lambda _e: (e.axes.pop(i), rerender()))

        fields = pn.Column()
        for f in axis_param_schema(ax.type):
            fields.append(self._param_widget(ax, f, status, rerender))
        return pn.Column(
            pn.Row(header, pn.layout.HSpacer(), up, down, rm), fields, pn.layout.Divider(),
            styles={"background": "#f6f8fa", "padding": "6px 10px", "border-radius": "6px"},
            margin=(0, 0, 6, 0))

    def _param_widget(self, ax, f, status, rerender):
        cur = _get(ax.params, f.key)
        if cur is None:
            cur = f.default
        if f.kind == "float":
            w = pn.widgets.FloatInput(name=f.label, value=float(cur or 0))
        elif f.kind == "int":
            w = pn.widgets.IntInput(name=f.label, value=int(cur or 0))
        elif f.kind == "bool":
            w = pn.widgets.Checkbox(name=f.label, value=bool(cur))
        elif f.kind == "floatlist":
            w = pn.widgets.TextInput(name=f.label, value=_fmt_floatlist(cur))
        else:
            w = pn.widgets.TextInput(name=f.label, value=str(cur if cur is not None else ""))

        def _apply(_ev):
            v = _parse_floatlist(w.value) if f.kind == "floatlist" else w.value
            _set(ax.params, f.key, v)
            rerender()
        w.param.watch(_apply, "value")
        return w

    def _manual_card(self, e):
        inner = pn.Column()

        def _render():
            inner.clear()
            if not e.manual_setup:
                inner.append(pn.pane.Markdown("_none_"))
            for i, step in enumerate(e.manual_setup):
                prompt = pn.widgets.TextInput(name="Prompt", value=step.prompt)
                names = pn.widgets.TextInput(name="Capture signals (comma)",
                                             value=", ".join(v["name"] for v in step.values))
                rm = pn.widgets.Button(name="✕", button_type="danger", width=36)

                def _apply(_ev, step=step, prompt=prompt, names=names):
                    step.prompt = prompt.value
                    step.values = [{"name": n.strip(), "cast": "float"}
                                   for n in names.value.split(",") if n.strip()]
                    self._render_script()
                prompt.param.watch(_apply, "value")
                names.param.watch(_apply, "value")
                rm.on_click(lambda _e, idx=i: (e.manual_setup.pop(idx), _render()))
                inner.append(pn.Row(prompt, names, rm))
            self._render_script()

        from smi_acquire.spec import ManualSetupStep
        add = pn.widgets.Button(name="+ manual setup step", width=200)
        add.on_click(lambda _e: (e.manual_setup.append(ManualSetupStep(
            prompt="Confirm the next condition", values=[{"name": "value_1", "cast": "float"}])),
            _render()))
        _render()
        return _card("Manual setup steps (→ recorded Signals)", inner, add)

    # ---- the interview (non-reloading) -------------------------------
    def _interview_card(self, target):
        widgets = {}
        rows = {}
        container = pn.Column()

        for q in interview.INTAKE:
            w = self._intake_widget(q)
            widgets[q.key] = w
            row = pn.Column(pn.pane.Markdown("**{}**".format(q.prompt)), w)
            if q.help:
                row.insert(1, pn.pane.Markdown(
                    "<span style='color:#777;font-size:12px'>{}</span>".format(q.help)))
            rows[q.key] = row
            container.append(row)

        def _answers():
            return {k: w.value for k, w in widgets.items()}

        def _refresh_visibility(*_):
            a = _answers()
            for q in interview.INTAKE:
                rows[q.key].visible = q.visible(a)

        for w in widgets.values():
            w.param.watch(_refresh_visibility, "value")
        _refresh_visibility()

        seed_btn = pn.widgets.Button(name="◆ seed experiment from answers",
                                     button_type="primary", width=260)

        def _seed(_e):
            spec = interview.seed_spec_from_intake(_answers())
            e = self.current_exp
            if e is None:
                e = Experiment.from_spec(spec, name="experiment {}".format(
                    len(self.project.experiments) + 1))
                self.project.experiments.append(e)
                self.current_exp = e
            else:
                e.beam, e.apparatus = spec.beam, spec.apparatus
                e.axes, e.manual_setup = spec.axes, spec.manual_setup
                e.scan_name = spec.scan_name
            self._refresh_experiment_list()
            self._render_editor()
            self.refresh_spine()
            _toast("seeded '{}'".format(e.name))
        seed_btn.on_click(_seed)

        return pn.Card(container, seed_btn, title="Seed from a short interview (optional)",
                       collapsed=(target is not None and bool(target.axes)))

    def _intake_widget(self, q):
        if q.kind == "text":
            return pn.widgets.TextInput(value=q.default or "")
        if q.kind == "bool":
            return pn.widgets.Checkbox(value=bool(q.default), name="yes")
        if q.kind == "choice":
            opts = {label: val for val, label in q.options}
            return pn.widgets.RadioBoxGroup(options=opts, value=q.default or list(opts.values())[0])
        if q.kind == "multichoice":
            opts = {label: val for val, label in q.options}
            return pn.widgets.CheckBoxGroup(options=opts, value=list(q.default or []))
        return pn.widgets.TextInput(value="")

    # ---- script + dry-run --------------------------------------------
    def _render_script(self):
        e = self.current_exp
        if e is None:
            self.code.value = ""
            return
        try:
            self.code.value = codegen.render_experiment(self.project, e, self.store)
        except Exception as exc:
            self.code.value = "# ERROR: {}".format(exc)

    def _validate(self):
        e = self.current_exp
        if e is None:
            self.report.object = "_No experiment selected._"
            return
        self._render_script()
        rep = dryrun.dry_run_experiment(self.project, e, self.store)
        targeted = self.project.resolve_target(e, self.store)
        unpos = [s.name for s in targeted if not self._is_positioned(s)]
        lines = ["### {}".format(rep.summary()),
                 "_targets {} sample(s)_".format(len(targeted))]
        if unpos:
            lines.append("⚠️ unpositioned (will measure at current stage): " + ", ".join(unpos))
        if rep.error:
            lines.append("```\n{}\n```".format(rep.error))
        for w in rep.warnings:
            lines.append("- ⚠️ {}".format(w))
        self.report.object = "\n\n".join(lines)

    # ---- submission seam (copy-paste now / queueserver later) ---------
    def _submit_copy(self):
        """Hand the generated script to the executor; for the local backend this is copy-paste."""
        e = self.current_exp
        if e is None:
            self.submit_status.object = "_No experiment selected._"
            return
        self._render_script()
        try:
            sub = self.executor.submit(self.code.value)
        except InterlockedError as exc:
            self.submit_status.object = "❌ {}".format(exc)
            return
        if sub.kind == "copy":
            # Put it on the clipboard via the code editor selection + a clear instruction.
            self.submit_status.object = (
                "📋 **Script ready** — select-all in the box below and copy, then paste into the "
                "beamline IPython session and run the final `RE(...)` line. _{}_".format(sub.detail))
            _toast("script ready to copy into the beamline session")
        elif sub.ok:
            self.submit_status.object = "✅ {} — {}".format(sub.text, sub.detail)
        else:
            self.submit_status.object = "❌ {}".format(sub.text)

    def _submit_queue(self):
        """Stub: enqueue to the queueserver (disabled until qserver exists)."""
        try:
            QueueServerExecutor().submit(None)
        except NotImplementedError as exc:
            self.submit_status.object = "⇪ queueserver not available: {}".format(exc)
            _toast("queueserver backend is not built yet", "warning")

    # ==================================================================
    # assemble
    # ==================================================================
    def _build(self):
        self._build_spine()
        self._build_home()
        self._build_plan()

        self.tabs = pn.Tabs(("Align & Samples", self.home), ("Plan", self.plan), dynamic=False)
        self.tabs.param.watch(self._on_tab, "active")

        self.template = pn.template.FastListTemplate(
            title="SMI-SWAXS Acquire — samples are the spine",
            accent_base_color=ACCENT, header_background=ACCENT,
            sidebar=[self.spine_panel,
                     pn.pane.Markdown("---\n_No hardware: microscope → fake IOC; "
                                      "validation → simulated beamline._")],
            sidebar_width=380, main=[self.tabs],
        )

    def _on_tab(self, event):
        if event.new == 0:
            self._ensure_microscope()
        elif event.new == 1:
            self._render_script()

    def servable(self):
        # start the microscope eagerly so the home tab is live on load
        pn.state.onload(self._ensure_microscope)
        return self.template.servable(title="smi-acquire")


def _card(title, *content):
    return pn.Column(pn.pane.Markdown("### {}".format(title)), *content,
                     pn.layout.Divider(), margin=(0, 0, 10, 0))


AcquireApp().servable()
