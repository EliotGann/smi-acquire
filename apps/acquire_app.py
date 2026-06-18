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

from smi_acquire import codegen, dryrun, registry
from smi_acquire.interview import axis_param_schema, default_axis
from smi_acquire.project import Project, Experiment, Reference
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


# ---------------------------------------------------------------------------
# wizard visual helpers (iconic cards, pills, color tints)
# ---------------------------------------------------------------------------
_SPEED_LABEL = {0: "fast", 1: "med", 2: "slow"}
_SPEED_BG = {0: "#66BB6A", 1: "#FFB74D", 2: "#EF5350"}


def _tint(color, alpha="22"):
    """A low-alpha version of a ``#RRGGBB`` color for soft backgrounds."""
    c = (color or "#607D8B").strip()
    if len(c) == 7 and c.startswith("#"):
        return c + alpha
    return c


def _pill(text, bg, fg="#fff"):
    return ("<span style='display:inline-block;padding:1px 8px;border-radius:10px;"
            "background:{};color:{};font-size:11px;font-weight:600;"
            "white-space:nowrap'>{}</span>").format(bg, fg, text)


def _speed_pill(speed):
    return _pill(_SPEED_LABEL.get(int(speed), "?"), _SPEED_BG.get(int(speed), "#90A4AE"))


def _icon_card_button(icon, label, color, selected, *, sub="", width=215, height=120):
    """A big iconic selectable card rendered as a styled Button (Panel buttons render unicode).

    Selected → filled with ``color``; unselected → outlined/gray. Returns the Button so the
    caller can wire ``.on_click``.
    """
    name = "{}\n{}".format(icon, label)
    if sub:
        name += "\n{}".format(sub)
    if selected:
        sheet = (":host {{ }} .bk-btn, button {{"
                 "background: {col} !important; color: #fff !important;"
                 "border: 2px solid {col} !important; border-radius: 12px !important;"
                 "font-size: 13px !important; font-weight: 600 !important;"
                 "white-space: pre-line !important; line-height: 1.5 !important;"
                 "box-shadow: 0 2px 8px {col}55 !important; }}").format(col=color)
    else:
        sheet = (".bk-btn, button {"
                 "background: #fff !important; color: #455A64 !important;"
                 "border: 2px solid #CFD8DC !important; border-radius: 12px !important;"
                 "font-size: 13px !important; font-weight: 500 !important;"
                 "white-space: pre-line !important; line-height: 1.5 !important; }")
    return pn.widgets.Button(name=name, width=width, height=height,
                             stylesheets=[sheet], margin=(6, 8, 6, 0))


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
        """All configured stage axes as ``{Position-field: value}`` (piezo_* + stage_*).

        Reads the full stacked stage so a captured position records every axis the microscope
        exposes; falls back to the primary x/y/z when only those are configured.
        """
        if self.micro is None:
            return {}
        try:
            return self.micro.stage.read_all_axes()
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
        # Full captured position of the selected sample (all piezo_* + stage_* axes).
        self.sample_detail = pn.pane.Markdown("_select a sample to see its full position_")
        self.spine.param.watch(lambda _e: self._refresh_sample_detail(), "selection")
        self.spine_panel = pn.Column(
            pn.pane.Markdown("### Sample list"),
            self.store_status,
            self.spine_count,
            self.spine,
            pn.Row(add, rm),
            pn.Row(load),
            self.sample_detail,
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
        self._refresh_sample_detail()
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

    # Position-field display: label + units (piezo µm, Huber mm/deg).
    _POS_FIELDS = [
        ("piezo_x", "piezo x", "µm"), ("piezo_y", "piezo y", "µm"),
        ("piezo_z", "piezo z", "µm"), ("piezo_th", "piezo θ", "°"),
        ("piezo_chi", "piezo χ", "°"),
        ("stage_x", "huber x", "mm"), ("stage_y", "huber y", "mm"),
        ("stage_z", "huber z", "mm"), ("stage_theta", "huber θ", "°"),
        ("stage_chi", "huber χ", "°"), ("stage_phi", "huber φ", "°"),
    ]

    def _fmt_position(self, pos):
        """One-line summary of the set axes of a Position (with units)."""
        if pos is None:
            return "—"
        bits = []
        for field, label, unit in self._POS_FIELDS:
            v = getattr(pos, field, None)
            if v is not None:
                bits.append(f"{label} {v:g}{unit}")
        ia = list(getattr(pos, "incident_angles", []) or [])
        if ia:
            bits.append("ai " + " ".join(f"{a:g}" for a in ia))
        return " · ".join(bits) if bits else "_(no axes set)_"

    def _refresh_sample_detail(self):
        """Surface the full captured position (all axes) of the selected sample."""
        if not hasattr(self, "sample_detail"):
            return
        s = self._selected_sample()
        if s is None:
            self.sample_detail.object = "_select a sample to see its full position_"
            return
        lines = [f"**{s.name}** — full captured position",
                 "nominal: " + self._fmt_position(s.nominal)]
        if s.refined is not None:
            lines.append("refined ✓: " + self._fmt_position(s.refined))
        self.sample_detail.object = "  \n".join(lines)

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
            ui = build_microscope(executor=self.executor, interlock=self.interlock)
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

    @staticmethod
    def _primary_xyz(ax: dict):
        """The display/reference x/y/z from a full-axis dict (piezo_*, else stage_*)."""
        def pick(a):
            return ax.get("piezo_" + a, ax.get("stage_" + a))
        return pick("x"), pick("y"), pick("z")

    def _refresh_pos(self):
        ax = self._stage_axes()
        if ax:
            x, y, z = self._primary_xyz(ax)
            self.pos_readout.object = "position: **x {} · y {} · z {}**".format(x, y, z)

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
        x, y, z = self._primary_xyz(ax)
        name = (self.capture_name.value or "").strip() or "ref{}".format(
            len(self.project.references) + 1)
        self.project.references.append(Reference(name=name, x=x, y=y, z=z))
        self.capture_name.value = ""
        self.refresh_spine()
        _toast("added reference '{}'".format(name))

    # ==================================================================
    # PLAN — the visual scan-building WIZARD
    # ==================================================================
    def _build_wizard(self):
        """Create the wizard state + its container, seeding from the current experiment if any."""
        from smi_acquire.wizard import WizardState
        if self.current_exp is not None:
            self.wizard = WizardState.from_experiment(self.current_exp)
        else:
            self.wizard = WizardState()
        self.wizard_box = pn.Column(sizing_mode="stretch_width")
        self._wiz_edit_open = {}   # axis_type -> bool (inline-edit toggles in Compose)
        # Shared script/dry-run/submit widgets (reused by the Review step).
        self.code = pn.widgets.CodeEditor(language="python", theme="monokai", height=360,
                                          readonly=True, sizing_mode="stretch_width")
        self.report = pn.pane.Markdown("")
        self.submit_status = pn.pane.Markdown("")
        self._render_wizard()

    # ---- top-level renderer: chrome + the active step -------------------
    def _render_wizard(self):
        self.wizard_box.clear()
        st = self.wizard
        body = {
            "measure": self._wiz_measure,
            "change": self._wiz_change,
            "configure": self._wiz_configure,
            "compose": self._wiz_compose,
            "review": self._wiz_review,
        }[st.step]()
        self.wizard_box.extend([self._wiz_rail(), body, self._wiz_nav()])

    # ---- progress rail (clickable past steps) --------------------------
    def _wiz_rail(self):
        from smi_acquire import wizard
        st = self.wizard
        pills = []
        for i, key in enumerate(wizard.STEPS):
            label = "{} {}".format(wizard.STEP_ICONS[key], wizard.STEP_TITLES[key])
            reachable = i <= st.step_index
            if i == st.step_index:
                sheet = (".bk-btn, button { background:%s !important; color:#fff !important;"
                         "border:none !important; border-radius:16px !important;"
                         "font-weight:700 !important; }" % ACCENT)
            elif reachable:
                sheet = (".bk-btn, button { background:#E3F2FD !important; color:%s !important;"
                         "border:1px solid %s !important; border-radius:16px !important; }"
                         % (ACCENT, ACCENT))
            else:
                sheet = (".bk-btn, button { background:#ECEFF1 !important; color:#B0BEC5 "
                         "!important; border:none !important; border-radius:16px !important; }")
            b = pn.widgets.Button(name=label, height=34, stylesheets=[sheet],
                                  disabled=(i > st.step_index), margin=(0, 4, 0, 0))
            b.on_click(lambda _e, idx=i: self._wiz_goto(idx))
            pills.append(b)
        return pn.Column(pn.Row(*pills, sizing_mode="stretch_width"),
                         pn.layout.Divider(), margin=(0, 0, 4, 0))

    def _wiz_goto(self, idx):
        self.wizard.goto(idx)
        self._render_wizard()

    # ---- bottom Back / Next chrome -------------------------------------
    def _wiz_nav(self):
        from smi_acquire import wizard
        st = self.wizard
        back = pn.widgets.Button(name="← Back", width=110, disabled=(st.step_index == 0))
        back.on_click(lambda _e: self._wiz_back())
        reset = pn.widgets.Button(name="↺ start over", button_type="warning", width=130)
        reset.on_click(lambda _e: self._wiz_reset())
        right = [pn.layout.HSpacer(), reset]
        if st.step_index < len(wizard.STEPS) - 1:
            nxt = pn.widgets.Button(name="Next →", button_type="primary", width=120,
                                    disabled=not st.can_advance())
            nxt.on_click(lambda _e: self._wiz_next())
            right.append(nxt)
        return pn.Column(pn.layout.Divider(),
                         pn.Row(back, *right, sizing_mode="stretch_width"))

    def _wiz_next(self):
        self.wizard.next()
        self._render_wizard()

    def _wiz_back(self):
        self.wizard.back()
        self._render_wizard()

    def _wiz_reset(self):
        from smi_acquire.wizard import WizardState
        self.wizard = WizardState()
        self._render_wizard()
        _toast("wizard reset")

    # ==================================================================
    # STEP 1 — Measure
    # ==================================================================
    def _wiz_measure(self):
        from smi_acquire import wizard
        st = self.wizard
        col = pn.Column(pn.pane.HTML("<h2>🔬 What do you want to measure?</h2>"),
                        sizing_mode="stretch_width")

        # Geometry cards
        col.append(pn.pane.HTML("<b>Geometry</b> — how the beam hits the sample"))
        geo_row = pn.Row()
        for g in wizard.GEOMETRIES:
            sel = (st.geometry == g.value)
            card = _icon_card_button(g.icon, g.label, ACCENT, sel, sub=g.blurb,
                                     width=300, height=130)
            card.on_click(lambda _e, v=g.value: self._wiz_set_geometry(v))
            geo_row.append(card)
        col.append(geo_row)

        # Q-range cards
        col.append(pn.pane.HTML("<b>q-range / detectors</b>"))
        q_row = pn.Row()
        for q in wizard.Q_RANGES:
            sel = (st.q == q.value)
            card = _icon_card_button(q.icon, q.label, "#5C6BC0", sel,
                                     sub=", ".join(q.detectors), width=230, height=120)
            card.on_click(lambda _e, v=q.value: self._wiz_set_q(v))
            q_row.append(card)
        col.append(q_row)

        # Exposure + names
        exp = pn.widgets.FloatInput(name="Exposure (s)", value=float(st.exposure_s),
                                    step=0.1, width=140)
        exp.param.watch(lambda e: self._wiz_assign("exposure_s", float(e.new)), "value")
        proj = pn.widgets.TextInput(name="Project name (optional)", value=st.project_name,
                                    width=220)
        proj.param.watch(lambda e: self._wiz_assign("project_name", e.new), "value")
        scan = pn.widgets.TextInput(name="Scan name (optional)", value=st.scan_name, width=220)
        scan.param.watch(lambda e: self._wiz_assign("scan_name", e.new), "value")
        col.append(pn.Row(exp, proj, scan))

        # Reflection-only: alignment in setup
        if st.geometry == "reflection":
            align_box = pn.Column(
                pn.pane.HTML("<b>📐 Alignment in setup?</b> (grazing geometry)"),
                styles={"background": _tint("#26A69A"), "padding": "8px 12px",
                        "border-radius": "10px", "border-left": "4px solid #26A69A"})
            routines = {"(none)": None, **{r: r for r in registry.ALIGNMENT_ROUTINES}}
            al = pn.widgets.Select(name="Alignment routine", options=routines,
                                   value=st.align_routine, width=300)
            al.param.watch(lambda e: self._wiz_assign("align_routine", e.new), "value")
            ang = pn.widgets.FloatInput(name="Align angle (deg)", value=float(st.align_angle),
                                        step=0.05, width=150)
            ang.param.watch(lambda e: self._wiz_assign("align_angle", float(e.new)), "value")
            align_box.append(pn.Row(al, ang))
            col.append(align_box)

        # Advanced: reads + attenuators
        reads = pn.widgets.MultiChoice(name="Record per event (reads)",
                                       options=registry.read_names(), value=list(st.reads))
        reads.param.watch(lambda e: self._wiz_assign("reads", list(e.new)), "value")
        atts = pn.widgets.MultiChoice(name="Attenuators in", options=registry.ATTENUATORS,
                                      value=list(st.attenuators_in))
        atts.param.watch(lambda e: self._wiz_assign("attenuators_in", list(e.new)), "value")
        col.append(pn.Card(reads, atts, title="Advanced (reads / attenuators)",
                           collapsed=True, sizing_mode="stretch_width"))
        return col

    def _wiz_set_geometry(self, value):
        self.wizard.geometry = value
        self._render_wizard()

    def _wiz_set_q(self, value):
        st = self.wizard
        st.q = value
        st.reads = list(st.beam_spec().reads)   # mirror q→reads defaults
        self._render_wizard()

    def _wiz_assign(self, attr, value):
        """Set a scalar wizard attribute without a full re-render (avoids losing focus)."""
        setattr(self.wizard, attr, value)

    # ==================================================================
    # STEP 2 — Change (iconic toggle cards)
    # ==================================================================
    def _wiz_change(self):
        from smi_acquire import wizard
        st = self.wizard
        col = pn.Column(
            pn.pane.HTML("<h2>🎛️ What do you want to change?</h2>"
                         "<span style='color:#777'>Pick the quantities to vary — each becomes "
                         "a nested scan layer. Zero is fine (a single point per sample).</span>"),
            sizing_mode="stretch_width")
        grid = pn.FlexBox(sizing_mode="stretch_width")
        for kind in wizard.changeables():
            on = st.has_change(kind.type)
            label = ("✓ " if on else "") + kind.label
            card = _icon_card_button(kind.icon, label, kind.color, on,
                                     sub=kind.blurb, width=235, height=150)
            card.on_click(lambda _e, t=kind.type: self._wiz_toggle(t))
            wrap = pn.Column(card, pn.pane.HTML(
                "<div style='margin:-4px 0 6px 2px'>{} {}</div>".format(
                    _speed_pill(kind.speed), self._needs_badge(kind))))
            grid.append(wrap)
        col.append(grid)
        col.append(pn.pane.HTML(
            "<div style='margin-top:8px;color:#555'>Currently changing: <b>{}</b></div>".format(
                ", ".join(a.type for a in st.axes) or "nothing (single point)")))
        return col

    def _needs_badge(self, kind):
        """A small note if this kind's prerequisites are not satisfied (still togglable)."""
        st = self.wizard
        msgs = []
        for need in kind.needs:
            if need == "reflection" and st.geometry != "reflection":
                msgs.append("needs grazing geometry")
            elif need == "heater":
                msgs.append("adds a heater")
        if not msgs:
            return ""
        return ("<span style='color:#E65100;font-size:11px'>⚠ {}</span>"
                .format("; ".join(msgs)))

    def _wiz_toggle(self, axis_type):
        self.wizard.toggle_change(axis_type)
        self._render_wizard()

    # ==================================================================
    # STEP 3 — Configure each change
    # ==================================================================
    def _wiz_configure(self):
        st = self.wizard
        col = pn.Column(pn.pane.HTML("<h2>🎚️ Configure each change</h2>"),
                        sizing_mode="stretch_width")
        if not st.axes:
            col.append(pn.pane.Alert(
                "Nothing to configure — you're measuring a single point per sample. "
                "Go **Next**.", alert_type="success"))
            return col
        for i, ax in enumerate(st.axes):
            col.append(self._wiz_config_card(i, ax))
        col.append(pn.pane.HTML(
            "<div style='color:#555'><b>{:,} events per sample</b></div>".format(
                st.events_per_sample())))
        return col

    def _wiz_config_card(self, i, ax):
        kind = registry.AXIS_KIND_BY_TYPE.get(ax.type)
        color = kind.color if kind else "#607D8B"
        icon = kind.icon if kind else "●"
        label = kind.label if kind else ax.type
        header = pn.pane.HTML(
            "<div style='background:{c};color:#fff;padding:6px 12px;border-radius:8px 8px 0 0;"
            "font-weight:600'>{ic} {lab} &nbsp;·&nbsp; {n} pts</div>".format(
                c=color, ic=icon, lab=label, n=ax.n_points()))
        fields = pn.Column(margin=(0, 0, 0, 0))
        # spatial: offer the shape selector first (recomputes x/y on change)
        if ax.type == "spatial":
            shape = self._spatial_shape(ax)
            sh = pn.widgets.RadioButtonGroup(
                name="Shape", options=["spot", "line", "grid"], value=shape)
            sh.param.watch(lambda e, idx=i: self._wiz_set_shape(idx, e.new), "value")
            fields.append(pn.Row(pn.pane.HTML("<b>Shape</b>"), sh))
        # incidence: a list-vs-range(start/stop/step) chooser instead of only an explicit list
        if ax.type == "incidence":
            fields.append(self._incidence_fields(i, ax))
        else:
            for f in axis_param_schema(ax.type):
                fields.append(self._param_widget(ax, f, None, self._render_wizard))
        return pn.Column(
            header,
            pn.Column(fields, styles={"background": _tint(color, "14"),
                                      "padding": "8px 12px",
                                      "border-radius": "0 0 8px 8px",
                                      "border": "1px solid " + _tint(color, "55")}),
            margin=(0, 0, 12, 0), sizing_mode="stretch_width")

    @staticmethod
    def _spatial_shape(ax):
        p = ax.params
        has_y = bool(p.get("y"))
        nx = len(p.get("x", []) or [])
        if has_y:
            return "grid"
        if nx > 6:
            return "line"
        return "spot"

    def _wiz_set_shape(self, i, shape):
        st = self.wizard
        if 0 <= i < len(st.axes) and st.axes[i].type == "spatial":
            new = default_axis("spatial", shape=shape)
            st.axes[i].params = new.params
        self._render_wizard()

    # ---- incidence: list vs range (start/stop/step) -------------------
    @staticmethod
    def _incidence_mode(ax):
        """'range' if the axis carries a [start,stop,step] range, else 'list'."""
        rng = ax.params.get("range")
        return "range" if (rng and len(rng) == 3) else "list"

    def _incidence_fields(self, i, ax):
        """A list-or-range chooser for grazing incidence angles."""
        mode = self._incidence_mode(ax)
        sel = pn.widgets.RadioButtonGroup(
            name="Angles", options=["list", "range"], value=mode, width=160)
        sel.param.watch(lambda e, idx=i: self._incidence_set_mode(idx, e.new), "value")
        body = pn.Column()
        if mode == "range":
            rng = list(ax.params.get("range") or [0.1, 0.4, 0.05])
            start = pn.widgets.FloatInput(name="start (deg)", value=float(rng[0]), step=0.01, width=120)
            stop = pn.widgets.FloatInput(name="stop (deg)", value=float(rng[1]), step=0.01, width=120)
            step = pn.widgets.FloatInput(name="step (deg)", value=float(rng[2]), step=0.01, width=120)

            def _apply(_e, idx=i, s=start, e2=stop, st=step):
                self._incidence_set_range(idx, s.value, e2.value, st.value)
            for w in (start, stop, step):
                w.param.watch(_apply, "value")
            n = len(ax.values())
            body.append(pn.Row(start, stop, step))
            body.append(pn.pane.HTML(
                "<span style='color:#555;font-size:12px'>→ {} angle(s): {}</span>".format(
                    n, ", ".join("{:g}".format(v) for v in ax.values()))))
        else:
            vals = pn.widgets.TextInput(
                name="Incident angles (deg, rel. to aligned 0)",
                value=_fmt_floatlist(ax.params.get("values") or []))
            vals.param.watch(lambda e, idx=i: self._incidence_set_list(idx, e.new), "value")
            body.append(vals)
        return pn.Column(pn.Row(pn.pane.HTML("<b>Incident angles</b>"), sel), body)

    def _incidence_set_mode(self, i, mode):
        st = self.wizard
        if not (0 <= i < len(st.axes)):
            return
        ax = st.axes[i]
        if mode == "range":
            # seed a range from the current values (or a sensible default) and drop the list
            cur = ax.values()
            if len(cur) >= 2:
                start, stop = cur[0], cur[-1]
                step = round((stop - start) / (len(cur) - 1), 4) or 0.05
            else:
                start, stop, step = 0.1, 0.4, 0.05
            ax.params["range"] = [start, stop, step]
            ax.params.pop("values", None)
        else:
            # materialize the current points as an explicit list and drop the range
            ax.params["values"] = ax.values()
            ax.params.pop("range", None)
        self._render_wizard()

    def _incidence_set_range(self, i, start, stop, step):
        st = self.wizard
        if 0 <= i < len(st.axes):
            st.axes[i].params["range"] = [float(start), float(stop), float(step)]
            st.axes[i].params.pop("values", None)
        self._render_wizard()

    def _incidence_set_list(self, i, text):
        st = self.wizard
        if 0 <= i < len(st.axes):
            st.axes[i].params["values"] = _parse_floatlist(text)
            st.axes[i].params.pop("range", None)
        self._render_wizard()

    # ==================================================================
    # STEP 4 — Compose & target (the nested-box canvas)
    # ==================================================================
    def _wiz_compose(self):
        from smi_acquire import wizard
        st = self.wizard
        col = pn.Column(pn.pane.HTML("<h2>🧩 Compose &amp; target</h2>"),
                        sizing_mode="stretch_width")
        warns = st.order_warnings()
        warned_types = self._warned_types(warns)
        if warns:
            auto = pn.widgets.Button(name="↓ auto-order (slow outermost)",
                                     button_type="primary", width=240)
            auto.on_click(lambda _e: self._wiz_autoorder())
            col.append(pn.Column(
                pn.pane.Alert("⚠️ **Ordering warnings**\n\n"
                              + "\n".join("- {}".format(w) for w in warns),
                              alert_type="warning"), auto))

        # The nested-box canvas.
        if not st.axes:
            col.append(pn.pane.Alert(
                "No changes — a single 📸 measurement per sample.", alert_type="success"))
        else:
            col.append(self._wiz_canvas(warned_types))

        # + add another change
        present = {a.type for a in st.axes}
        missing = [k for k in wizard.changeables() if k.type not in present]
        if missing:
            add_row = pn.Row(pn.pane.HTML("<b>+ add another change:</b>"),
                             align="center")
            for kind in missing:
                sheet = (".bk-btn, button { border:1px solid %s !important; color:%s !important;"
                         "background:#fff !important; border-radius:14px !important; }"
                         % (kind.color, kind.color))
                b = pn.widgets.Button(name="{} {}".format(kind.icon, kind.label), height=32,
                                      stylesheets=[sheet], margin=(0, 4, 4, 0))
                b.on_click(lambda _e, t=kind.type: self._wiz_toggle(t))
                add_row.append(b)
            col.append(add_row)

        # Live readout
        col.append(pn.pane.HTML(
            "<div style='margin:8px 0;font-size:16px'><b>{:,} events per sample</b></div>".format(
                st.events_per_sample())))

        # Target
        col.append(pn.layout.Divider())
        col.append(self._wiz_target())
        return col

    def _wiz_canvas(self, warned_types):
        """Render the axes as literally nested boxes; innermost holds the measure core."""
        st = self.wizard
        n = len(st.axes)
        # Build from innermost out so each box wraps the previous content.
        inner = self._wiz_measure_core()
        for i in range(n - 1, -1, -1):
            inner = self._wiz_axis_box(i, st.axes[i], inner,
                                       warned=(st.axes[i].type in warned_types))
        return inner

    def _wiz_measure_core(self):
        st = self.wizard
        ndet = len(st.beam_spec().detectors)
        return pn.pane.HTML(
            "<div style='background:#37474F;color:#fff;padding:12px;border-radius:10px;"
            "text-align:center;font-weight:600'>📸 Measure<br>"
            "<span style='font-weight:400;font-size:12px'>{} detector(s) · {}s</span></div>"
            .format(ndet, st.exposure_s), margin=(6, 0, 6, 0))

    def _wiz_axis_box(self, i, ax, inner, *, warned=False):
        st = self.wizard
        kind = registry.AXIS_KIND_BY_TYPE.get(ax.type)
        color = kind.color if kind else "#607D8B"
        icon = kind.icon if kind else "●"
        label = kind.label if kind else ax.type
        # cumulative outer product: points of this axis × all outer axes (0..i)
        moves = 1
        for j in range(i + 1):
            moves *= max(1, st.axes[j].n_points())

        warn_html = ""
        box_border = color
        bg = _tint(color, "22")
        if warned:
            box_border = "#E65100"
            bg = _tint("#FF7043", "33")
            warn_html = ("<span style='color:#BF360C;font-weight:700'> ⚠️ slow-inside-fast</span>")

        header = pn.pane.HTML(
            "<div style='font-weight:600;color:#37474F'>{ic} {lab} "
            "<span style='color:#666;font-weight:400'>× {n} pts</span> {badge}{warn}</div>"
            .format(ic=icon, lab=label, n=ax.n_points(),
                    badge=_pill("moves {}×".format(moves), color), warn=warn_html))

        up = pn.widgets.Button(name="▲", width=34, height=30, disabled=(i == 0))
        up.on_click(lambda _e, idx=i: self._wiz_move(idx, -1))
        down = pn.widgets.Button(name="▼", width=34, height=30,
                                 disabled=(i == len(st.axes) - 1))
        down.on_click(lambda _e, idx=i: self._wiz_move(idx, +1))
        edit = pn.widgets.Toggle(name="✎ edit", width=70, height=30,
                                 value=self._wiz_edit_open.get(ax.type, False))
        edit.param.watch(lambda e, t=ax.type: self._wiz_set_edit(t, e.new), "value")
        rm = pn.widgets.Button(name="✕", button_type="danger", width=34, height=30)
        rm.on_click(lambda _e, t=ax.type: self._wiz_remove(t))

        box = pn.Column(
            pn.Row(header, pn.layout.HSpacer(), up, down, edit, rm,
                   sizing_mode="stretch_width", align="center"),
            sizing_mode="stretch_width",
            styles={"background": bg, "border-left": "5px solid " + box_border,
                    "border-radius": "10px", "padding": "10px 12px",
                    "margin": "6px 0"})
        if self._wiz_edit_open.get(ax.type, False):
            box.append(self._wiz_inline_params(i, ax))
        box.append(inner)
        return box

    def _wiz_inline_params(self, i, ax):
        fields = pn.Column(styles={"background": "#ffffffaa", "padding": "6px 8px",
                                   "border-radius": "8px", "margin": "4px 0"})
        if ax.type == "spatial":
            sh = pn.widgets.RadioButtonGroup(options=["spot", "line", "grid"],
                                             value=self._spatial_shape(ax))
            sh.param.watch(lambda e, idx=i: self._wiz_set_shape(idx, e.new), "value")
            fields.append(pn.Row(pn.pane.HTML("<b>Shape</b>"), sh))
        if ax.type == "incidence":
            fields.append(self._incidence_fields(i, ax))
        else:
            for f in axis_param_schema(ax.type):
                fields.append(self._param_widget(ax, f, None, self._render_wizard))
        return fields

    def _wiz_move(self, i, delta):
        self.wizard.move_axis(i, delta)
        self._render_wizard()

    def _wiz_autoorder(self):
        self.wizard.auto_order()
        self._render_wizard()

    def _wiz_remove(self, axis_type):
        self.wizard.remove_change(axis_type)
        self._wiz_edit_open.pop(axis_type, None)
        self._render_wizard()

    def _wiz_set_edit(self, axis_type, value):
        self._wiz_edit_open[axis_type] = bool(value)
        self._render_wizard()

    @staticmethod
    def _warned_types(warns):
        """Axis types named (as label) in any order warning."""
        out = set()
        for kind in registry.AXIS_KINDS:
            for w in warns:
                if "'{}'".format(kind.type) in w or "'{}:".format(kind.type) in w:
                    out.add(kind.type)
        return out

    def _wiz_target(self):
        st = self.wizard
        col = pn.Column(pn.pane.HTML("<b>🎯 Run on</b>"))
        opts = self._target_options()         # {label: "all" | "holder:<id>"}
        cur = "all"
        if st.target_kind == "holder" and st.target_holder_id:
            cur = "holder:" + st.target_holder_id
        sel = pn.widgets.Select(options=opts, value=cur if cur in opts.values() else "all",
                                width=320)
        sel.param.watch(lambda e: self._wiz_set_target(e.new), "value")
        col.append(sel)
        # positioned readout
        samples = self._wiz_target_samples()
        npos = sum(1 for s in samples if self._is_positioned(s))
        col.append(pn.pane.HTML(
            "<span style='color:#2e7d32'>→ {} positioned sample(s) "
            "({} total in target)</span>".format(npos, len(samples))))
        return col

    def _wiz_set_target(self, val):
        st = self.wizard
        if val and val.startswith("holder:"):
            st.target_kind = "holder"
            st.target_holder_id = val[len("holder:"):]
        else:
            st.target_kind = "all"
            st.target_holder_id = None
        self._render_wizard()

    def _wiz_target_samples(self):
        st = self.wizard
        if st.target_kind == "holder" and st.target_holder_id:
            return self.store.list_samples(holder_id=st.target_holder_id)
        return self.store.list_samples()

    # ==================================================================
    # STEP 5 — Review & run
    # ==================================================================
    def _wiz_review(self):
        st = self.wizard
        # Apply the wizard onto a current Experiment (create one if needed).
        if self.current_exp is None:
            exp = st.to_experiment(name=st.suggested_scan_name() or "experiment 1")
            self.project.experiments.append(exp)
            self.current_exp = exp
        else:
            st.apply_to_experiment(self.current_exp)
        self.refresh_spine()
        self._render_script()

        col = pn.Column(pn.pane.HTML("<h2>🚀 Review &amp; run</h2>"),
                        sizing_mode="stretch_width")
        col.append(pn.pane.HTML(
            "<div style='font-size:15px;color:#37474F'>{}</div>".format(
                self._wiz_human_summary())))

        validate = pn.widgets.Button(name="Validate (dry-run)", button_type="primary",
                                     width=180)
        validate.on_click(lambda _e: self._validate())
        copy_btn = pn.widgets.Button(name="⧉ Copy RE command", button_type="success",
                                     width=200)
        copy_btn.on_click(lambda _e: self._submit_copy())
        submit_q = pn.widgets.Button(name="⇪ Submit to queue (qserver — N/A)",
                                     width=260, disabled=True)
        submit_q.on_click(lambda _e: self._submit_queue())
        col.append(pn.Row(validate, copy_btn, submit_q))
        col.append(self.submit_status)
        col.append(self.report)
        col.append(self.code)
        return col

    def _wiz_human_summary(self):
        from smi_acquire import wizard
        st = self.wizard
        geo = wizard.GEOMETRY_BY_VALUE.get(st.geometry)
        name = st.suggested_scan_name()
        axes = " × ".join(a.type for a in st.axes) or "single point"
        return "<b>{}</b> · {} · 1 run/sample · {:,} events".format(
            name, axes, st.events_per_sample()) + (
            "  ({})".format(geo.label) if geo else "")

    # ==================================================================
    # shared experiment/target helpers (reused by the wizard)
    # ==================================================================
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
        """Re-sync the wizard with the current experiment (used after loading a project)."""
        if not hasattr(self, "wizard"):
            return
        from smi_acquire.wizard import WizardState
        if self.current_exp is not None:
            self.wizard = WizardState.from_experiment(self.current_exp)
        else:
            self.wizard = WizardState()
        self._wiz_edit_open = {}
        self._render_wizard()

    def _param_widget(self, ax, f, status, rerender):
        """A single per-param widget bound to ``ax.params[f.key]``.

        ``status`` is unused here (kept for the old Plan caller signature); ``rerender`` is
        invoked after each edit (the wizard passes ``self._render_wizard``).
        """
        cur = _get(ax.params, f.key)
        if cur is None:
            cur = f.default
        help_txt = getattr(f, "help", "") or ""
        if f.kind == "float":
            w = pn.widgets.FloatInput(name=f.label, value=float(cur or 0), step=0.1)
        elif f.kind == "int":
            w = pn.widgets.IntInput(name=f.label, value=int(cur or 0))
        elif f.kind == "bool":
            w = pn.widgets.Checkbox(name=f.label, value=bool(cur))
        elif f.kind == "floatlist":
            w = pn.widgets.TextInput(name=f.label, value=_fmt_floatlist(cur),
                                     placeholder="space/comma separated")
        else:
            w = pn.widgets.TextInput(name=f.label, value=str(cur if cur is not None else ""))

        def _apply(_ev):
            v = _parse_floatlist(w.value) if f.kind == "floatlist" else w.value
            _set(ax.params, f.key, v)
            rerender()
        w.param.watch(_apply, "value")
        if help_txt:
            return pn.Column(w, pn.pane.HTML(
                "<span style='color:#888;font-size:11px'>{}</span>".format(help_txt)),
                margin=(0, 0, 2, 0))
        return w

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
        self._build_wizard()

        self.tabs = pn.Tabs(("Align & Samples", self.home), ("Plan", self.wizard_box),
                            dynamic=False)
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
            self._render_wizard()

    def servable(self):
        # start the microscope eagerly so the home tab is live on load
        pn.state.onload(self._ensure_microscope)
        return self.template.servable(title="smi-acquire")


AcquireApp().servable()
