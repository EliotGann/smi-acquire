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
from smi_acquire.lists import AcquireListStore
from smi_acquire.execute import LocalExecutor, QueueServerExecutor, InterlockedError
from smi_acquire.interlock import Interlock
from smi_acquire.proposal import Proposal

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


# A small palette to color successive energy regions distinctly (cycled).
_REGION_COLORS = ["#7E57C2", "#42A5F5", "#26A69A", "#FFA726", "#EF5350", "#66BB6A", "#8D6E63"]


def _region_color(base, r):
    """A distinct color for energy region ``r`` (cycles a small palette; base is region 0)."""
    if r == 0:
        return base or _REGION_COLORS[0]
    return _REGION_COLORS[r % len(_REGION_COLORS)]


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
        self.listdb = AcquireListStore.connect()   # live redis db=2 'swaxslists' named lists
        self.project = Project(name="")        # local: recipes + references only
        # Motion seam: jog directly via ophyd (interlock-gated); submit = copy-paste to the
        # beamline RunEngine.  The interlock reads the external RE-busy flag (db=3, read-only).
        self.interlock = Interlock.from_redis()
        self.proposal = Proposal.from_redis()      # read-only proposal/data-session for display
        self.executor = LocalExecutor(interlock=self.interlock)
        self.micro = None                 # MicroscopeUI (lazy: needs the IOC)
        self.current_exp: Experiment | None = None
        # Typed identity for each master-list row, parallel to the spine df:
        #   ("sample", sample_id) for store samples; ("ref", reference_id) for project refs.
        self._spine_rows: list[tuple[str, str]] = []
        self._build()

    # -- microscope marker sync --------------------------------------------
    def sync_markers(self):
        """Push the master list into the microscope engine: positioned store samples (lime) +
        local references (yellow). The engine renders the markers and exposes the scan targets;
        it owns no list of its own. Scan-target ticks (``in_scan``) are preserved across the push
        by name so toggling the master list's ``scan`` column survives a refresh.
        """
        if self.micro is None:
            return
        from smi_acquire.microscope.scripts import Bookmark as MBookmark
        inter = self.micro.interactive
        # Remember which sample names were ticked for scanning, to carry the flag across.
        try:
            ticked = {b.name for b in inter.get_scan_targets()}
        except Exception:
            ticked = set()
        entries = []
        for s in self.store.list_samples():
            xyz = self._sample_xyz(s)
            if xyz is None:
                continue
            x, y, z = xyz
            holder = self.store.holder_by_id(s.holder_id) if s.holder_id else None
            entries.append(MBookmark(name=s.name, x=x, y=y, z=z, is_reference=False,
                                     in_scan=s.name in ticked,
                                     holder=holder.name if holder is not None else None,
                                     sample_id=s.id))
        for r in self.project.references:
            if not r.visible:
                continue
            x, y, z = (r.x or 0.0), (r.y or 0.0), (r.z or 0.0)
            entries.append(MBookmark(name=r.name, x=x, y=y, z=z, is_reference=True))
        try:
            inter.set_samples(entries)
            if hasattr(self.micro, "wide"):
                self.micro.wide.set_samples(entries)
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
            value=self._spine_df(), show_index=False, selectable="checkbox", height=320,
            theme="simple",
            widths={"name": 96, "pri": 38, "project": 78, "holder": 76, "x": 52, "y": 52,
                    "z": 46, "incidence": 66, "scan": 42, "ref": 36, "active": 42, "md": 92},
            editors={"name": {"type": "input"}, "holder": {"type": "input"},
                     "incidence": {"type": "input"}, "md": {"type": "input"},
                     "project": {"type": "input"},
                     "pri": {"type": "number"},
                     "scan": {"type": "tickCross"},
                     "x": None, "y": None, "z": None, "ref": None, "active": None},
            formatters={"scan": {"type": "tickCross"}, "ref": {"type": "tickCross"}},
            titles={"pri": "▲"},
        )
        self.spine.on_edit(self._on_spine_edit)

        add = pn.widgets.Button(name="+ blank sample", width=120)
        add.on_click(self._on_add_blank)
        rm = pn.widgets.Button(name="✕ remove selected", button_type="danger", width=140)
        rm.on_click(self._on_remove_selected)
        load = pn.widgets.Button(name="◆ load (set active)", button_type="primary", width=160)
        load.on_click(self._on_set_active)
        goto = pn.widgets.Button(name="→ go to position", width=150)
        goto.on_click(self._on_goto_selected)

        # Run-order (priority) controls: ▲/▼ move in the displayed order; "set priority from
        # shown order" persists whatever order the user currently sees (including table sorts).
        pri_up = pn.widgets.Button(name="▲ up", width=70)
        pri_up.on_click(lambda _e: self._on_priority_move(-1))
        pri_down = pn.widgets.Button(name="▼ down", width=80)
        pri_down.on_click(lambda _e: self._on_priority_move(+1))
        pri_renum = pn.widgets.Button(name="⇅ priority = shown order", width=190)
        pri_renum.on_click(lambda _e: self._on_priority_renumber())
        refresh = pn.widgets.Button(name="↻ refresh", width=90)
        refresh.on_click(lambda _e: self.refresh_spine())

        # Holders sub-panel (replaces the old "Sets").
        new_holder = pn.widgets.TextInput(placeholder="new holder name…", width=130)
        mk_holder = pn.widgets.Button(name="+ holder", width=80)
        mk_holder.on_click(lambda _e: self._make_holder(new_holder))
        self.move_holder = pn.widgets.Select(options=self._holder_options(), width=140)
        move_btn = pn.widgets.Button(name="→ move sel. to holder", width=170)
        move_btn.on_click(self._on_move_holder)
        # Bulk-set the project of every sample on the selected sample's holder.
        self.holder_project = pn.widgets.TextInput(placeholder="project for whole holder…",
                                                   width=180)
        holder_project_btn = pn.widgets.Button(name="→ set holder's project", width=170)
        holder_project_btn.on_click(self._on_set_holder_project)
        rm_holder = pn.widgets.Button(name="remove holder", button_type="danger", width=120)
        rm_holder.on_click(lambda _e: self._on_remove_holder(delete_samples=False))
        clear_holders = pn.widgets.Button(name="clear holders", button_type="danger", width=120)
        clear_holders.on_click(lambda _e: self._on_clear_holders())

        self.adjust_axis = pn.widgets.Select(name="axis", options={
            "piezo x": "piezo_x", "piezo y": "piezo_y", "piezo z": "piezo_z",
            "huber x": "stage_x", "huber y": "stage_y", "huber z": "stage_z",
        }, width=115)
        self.adjust_mode = pn.widgets.RadioButtonGroup(options={"+ relative": "relative", "= absolute": "absolute"},
                                                       value="relative", width=175)
        self.adjust_value = pn.widgets.FloatInput(name="value", value=0.0, step=0.1, width=95)
        self.adjust_clear_refined = pn.widgets.Checkbox(name="clear refined", value=False, width=120)
        adjust_btn = pn.widgets.Button(name="apply to selected", button_type="warning", width=145)
        adjust_btn.on_click(self._on_adjust_positions)

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
        self.proposal_status = pn.pane.Markdown("")
        self._refresh_proposal_status()
        self.spine_count = pn.pane.Markdown("")
        self._refresh_spine_count()
        # Full captured position of the selected sample (all piezo_* + stage_* axes).
        self.sample_detail = pn.pane.Markdown("_select a sample to see its full position_")
        self.spine.param.watch(lambda _e: self._refresh_sample_detail(), "selection")
        self.spine_panel = pn.Column(
            pn.pane.Markdown("### Sample list"),
            self.store_status,
            self.proposal_status,
            self.spine_count,
            self.spine,
            pn.Row(add, rm),
            pn.Row(load, goto, refresh),
            pn.Row(pri_up, pri_down, pri_renum),
            pn.pane.Markdown(
                "<span style='color:#777;font-size:11px'>The list is shown in <b>run order</b> "
                "(<b>pri</b> column, lowest first); <b>▲/▼</b> move the selected sample, "
                "<b>priority = shown order</b> writes priorities from the current displayed table "
                "order. Tick <b>scan</b> "
                "to include a sample as a Scan-tab target; <b>ref</b> marks a reference landmark "
                "(never scanned). <b>go to position</b> moves the stage to the selected row.</span>"),
            self.sample_detail,
            pn.layout.Divider(),
            pn.pane.Markdown("**Adjust nominal positions**"),
            pn.Row(self.adjust_axis, self.adjust_value),
            pn.Row(self.adjust_mode, self.adjust_clear_refined),
            pn.Row(adjust_btn),
            pn.pane.Markdown(
                "<span style='color:#777;font-size:11px'>Applies only to selected samples' "
                "<b>nominal</b> positions. Refined positions are not changed; clear them if the "
                "nominal shift invalidates alignment.</span>"),
            pn.layout.Divider(),
            pn.pane.Markdown("**Holders**"),
            pn.Row(new_holder, mk_holder),
            pn.Row(self.move_holder, move_btn),
            pn.Row(self.holder_project, holder_project_btn),
            pn.Row(rm_holder, clear_holders),
            pn.layout.Divider(),
            pn.pane.Markdown("**Import / export**"),
            pn.Row(imp),
            pn.Row(self.export_csv, self.export_json),
            pn.Row(pn.pane.Markdown("project recipes:"), load_json),
        )

    def _spine_df(self):
        """Build the single master list: store samples + project references, flagged.

        ``_spine_rows`` is the parallel typed identity for each row — ``("sample", id)`` or
        ``("ref", id)`` — so selection actions know what each selected row is.  The ``scan``
        flag is read back from the microscope engine (where per-session scan-target state
        lives); ``ref`` rows are never scan targets.  Samples are **sorted by priority** (lower
        runs first) — the displayed order IS the run order; references trail after.
        """
        active = self.store.active_sample()
        active_id = active.id if active is not None else None
        scan_names = self._scan_ticked_names()
        self._spine_rows: list[tuple[str, str]] = []
        rows = []
        # Stable sort by priority (lower first); equal priority keeps store order.
        samples = sorted(self.store.list_samples(), key=self._sample_priority)
        for s in samples:
            self._spine_rows.append(("sample", s.id))
            holder = self.store.holder_by_id(s.holder_id) if s.holder_id else None
            p = s.nominal
            x = p.piezo_x if p.piezo_x is not None else p.stage_x
            y = p.piezo_y if p.piezo_y is not None else p.stage_y
            z = p.piezo_z if p.piezo_z is not None else p.stage_z
            angles = s.incident_angles or p.incident_angles
            rows.append({
                "name": s.name,
                "pri": self._sample_priority(s),
                "project": self._sample_project(s),
                "holder": holder.name if holder is not None else "",
                "x": x, "y": y, "z": z,
                "incidence": _fmt_floatlist(angles),
                "scan": s.name in scan_names,
                "ref": False,
                "active": "◆" if s.id == active_id else "",
                "md": self._display_md(s.md),
            })
        # References (local project fiducials) appear in the same list, flagged ref=✓.
        for r in self.project.references:
            self._spine_rows.append(("ref", r.id))
            rows.append({
                "name": r.name,
                "pri": "",
                "project": "",
                "holder": "",
                "x": r.x, "y": r.y, "z": r.z,
                "incidence": "",
                "scan": False,
                "ref": True,
                "active": "",
                "md": "",
            })
        return pd.DataFrame(
            rows, columns=["name", "pri", "project", "holder", "x", "y", "z", "incidence",
                           "scan", "ref", "active", "md"])

    @staticmethod
    def _sample_priority(sample) -> int:
        """The sample's run-order priority (lower runs first; default 0).

        Stored on ``Sample.md['priority']`` as a stopgap until ``smi_plans.Sample`` gains a
        native ``priority`` field (which will also make ``load_holder`` order by it). See the
        cross-repo note in docs/DESIGN.md.
        """
        try:
            return int(sample.md.get("priority", 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _sample_project(sample) -> str:
        """The sample's project_name (``Sample.md['project_name']``, default "").

        Per-sample so project can vary across a bar; carried into each run's md by acquire_bar.
        Every scan should carry a project_name (the experiment/project name is the fallback).
        """
        v = (sample.md or {}).get("project_name")
        return str(v) if v else ""

    @staticmethod
    def _display_md(md) -> str:
        """The md JSON shown in the spine, minus the fields that have their own columns."""
        if not md:
            return ""
        shown = {k: v for k, v in md.items() if k not in ("priority", "project_name")}
        return json.dumps(shown) if shown else ""


    def _scan_ticked_names(self) -> set:
        """Names of samples currently ticked as scan targets (held by the microscope engine)."""
        if self.micro is None:
            return set()
        try:
            return {b.name for b in self.micro.interactive.get_scan_targets()}
        except Exception:
            return set()


    def refresh_spine(self):
        selected = set(self._selected_rows()) if getattr(self, "_spine_rows", None) else set()
        self.spine.value = self._spine_df()
        if selected:
            self.spine.selection = [i for i, row in enumerate(self._spine_rows) if row in selected]
        self.move_holder.options = self._holder_options()
        if hasattr(self, "capture_holder"):
            self.capture_holder.options = self._holder_options()
        self._refresh_store_status()
        self._refresh_spine_count()
        self._refresh_sample_detail()
        if hasattr(self, "target_select"):
            self.target_select.options = self._target_options()
        self.sync_markers()

    def _poll_store_refresh(self):
        """Refresh the master list from the shared store so external Redis edits appear promptly."""
        try:
            self.refresh_spine()
        except Exception:
            pass

    def _refresh_store_status(self):
        if self.store.live:
            self.store_status.object = ("<span style='color:#2e7d32'>● live: {}</span>"
                                        .format(self.store.location))
        else:
            self.store_status.object = ("<span style='color:#b26a00'>○ {}</span>"
                                        .format(self.store.location))

    def _refresh_proposal_status(self):
        """Show the proposal/data-session read-only (it is set in the beamline session, not here)."""
        prop = None
        try:
            prop = self.proposal.current()
        except Exception:
            prop = None
        if prop:
            self.proposal_status.object = (
                "<span style='color:#37474F'>proposal: <b>{}</b> "
                "<span style='color:#999;font-size:11px'>(read-only; set in the beamline "
                "session)</span></span>".format(prop))
        else:
            self.proposal_status.object = (
                "<span style='color:#999;font-size:12px'>proposal: — (set in the beamline "
                "session; project_name travels per sample)</span>")

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

    def _selected_indices(self):
        """The selected master-list rows (sorted), clamped to valid rows."""
        return sorted(int(i) for i in (self.spine.selection or [])
                      if 0 <= int(i) < len(self._spine_rows))

    def _selected_rows(self):
        """The selected rows as typed ``(kind, id)`` pairs (``"sample"`` / ``"ref"``)."""
        return [self._spine_rows[i] for i in self._selected_indices()]

    def _selected_samples(self):
        """All selected *store samples* (reference rows are skipped)."""
        out = []
        for kind, rid in self._selected_rows():
            if kind != "sample":
                continue
            s = self.store.sample_by_id(rid)
            if s is not None:
                out.append(s)
        return out

    def _selected_sample(self):
        """The single (first) selected store sample -- single-target actions / detail panel."""
        for kind, rid in self._selected_rows():
            if kind == "sample":
                return self.store.sample_by_id(rid)
        return None

    def _selected_reference(self):
        """The single (first) selected project reference, or None."""
        for kind, rid in self._selected_rows():
            if kind == "ref":
                return next((r for r in self.project.references if r.id == rid), None)
        return None

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
        """Surface the full captured position (all axes) of the selected sample/reference."""
        if not hasattr(self, "sample_detail"):
            return
        rows = self._selected_rows()
        if not rows:
            self.sample_detail.object = "_select a row to see its full position_"
            return
        samples = self._selected_samples()
        if len(rows) > 1:
            self.sample_detail.object = (
                "**{} rows selected** — remove / move to holder act on all samples; "
                "load / go to use the first.".format(len(rows)))
            return
        kind, rid = rows[0]
        if kind == "ref":
            r = next((r for r in self.project.references if r.id == rid), None)
            if r is None:
                self.sample_detail.object = "_reference not found_"
                return
            self.sample_detail.object = (
                "**{}** — reference landmark  \nx {} · y {} · z {}".format(
                    r.name, r.x, r.y, r.z))
            return
        if not samples:
            self.sample_detail.object = "_select a row to see its full position_"
            return
        s = samples[0]
        lines = [f"**{s.name}** — full captured position",
                 "nominal: " + self._fmt_position(s.nominal)]
        if s.refined is not None:
            lines.append("refined ✓: " + self._fmt_position(s.refined))
        self.sample_detail.object = "  \n".join(lines)

    def _on_spine_edit(self, event):
        i = int(getattr(event, "row", -1))
        col = getattr(event, "column", None)
        val = getattr(event, "value", None)
        if not (0 <= i < len(self._spine_rows)):
            return
        kind, rid = self._spine_rows[i]

        # The "scan" tick is per-session state held by the microscope engine, and only
        # applies to real samples (references are never scan targets).
        if col == "scan":
            if kind == "sample":
                s = self.store.sample_by_id(rid)
                self._set_sample_scan(s.name if s is not None else None, bool(val))
            # Re-render either way so a stray tick on a reference row is reset.
            self.refresh_spine()
            return

        if kind == "ref":
            self._edit_reference(rid, col, val)
            self.refresh_spine()
            return

        s = self.store.sample_by_id(rid)
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
        elif col == "pri":
            self._set_sample_priority(s, val)
        elif col == "project":
            self._set_sample_project(s, val)
        elif col == "md":
            try:
                new_md = json.loads(val) if str(val).strip() else {}
                # The md column hides fields that have their own columns; preserve them.
                for hidden in ("priority", "project_name"):
                    if hidden in s.md and hidden not in new_md:
                        new_md[hidden] = s.md[hidden]
                s.md = new_md
                self.store.update_sample(s)
            except Exception:
                _toast("metadata must be JSON", "warning")
        self.refresh_spine()

    # ---- run-order (priority) -------------------------------------------
    def _update_sample_md(self, sample, key, value):
        """Set/clear ONE md key on the **live** sample (re-fetched), preserving other md fields.

        Re-reading by id before writing avoids clobbering concurrently-set md keys (priority /
        project_name / user fields) when a caller holds a stale ``Sample`` snapshot.
        """
        live = self.store.sample_by_id(getattr(sample, "id", None)) or sample
        live.md = dict(live.md or {})
        if value is None:
            live.md.pop(key, None)
        else:
            live.md[key] = value
        self.store.update_sample(live)
        return live

    def _set_sample_priority(self, sample, value, *, refresh=False):
        """Persist a sample's run-order priority on ``Sample.md['priority']`` (stopgap)."""
        try:
            pri = int(round(float(value)))
        except (TypeError, ValueError):
            _toast("priority must be a whole number", "warning")
            return
        self._update_sample_md(sample, "priority", pri)
        if refresh:
            self.refresh_spine()

    def _on_priority_move(self, delta):
        """Move the selected sample one step in run order by swapping priority with its neighbor.

        Operates on the priority-sorted sample order (what the list shows). Renumbers the samples
        to a dense 1..N first so a swap is always well-defined even if priorities were sparse/equal.
        """
        sel = self._selected_samples()
        if len(sel) != 1:
            _toast("select exactly one sample to move", "warning")
            return
        target = sel[0]
        ordered = sorted(self.store.list_samples(), key=self._sample_priority)
        # Dense renumber (1..N) in current order so positions are unambiguous.
        for n, s in enumerate(ordered, start=1):
            if self._sample_priority(s) != n:
                self._set_sample_priority(s, n)
        pos = next((k for k, s in enumerate(ordered) if s.id == target.id), None)
        if pos is None:
            return
        swap = pos + delta
        if not (0 <= swap < len(ordered)):
            return  # already at an end
        a, b = ordered[pos], ordered[swap]
        # swap their (now dense) priorities
        self._set_sample_priority(a, swap + 1)
        self._set_sample_priority(b, pos + 1)
        self.refresh_spine()
        # keep the moved sample selected at its new row
        new_order = [rid for kind, rid in self._spine_rows if kind == "sample"]
        if target.id in new_order:
            idx = next(i for i, (kind, rid) in enumerate(self._spine_rows)
                       if kind == "sample" and rid == target.id)
            self.spine.selection = [idx]

    def _on_priority_renumber(self):
        """Set priorities to the currently displayed sample order (including UI sorts)."""
        ordered = self._displayed_samples()
        for n, s in enumerate(ordered, start=1):
            if self._sample_priority(s) != n:
                self._set_sample_priority(s, n)
        self.refresh_spine()
        _toast("set priority from displayed order for {} sample(s)".format(len(ordered)))

    def _displayed_samples(self):
        """Samples in the order currently shown by Tabulator, falling back to spine rows."""
        by_name = {}
        for s in self.store.list_samples():
            by_name.setdefault(s.name, []).append(s)
        ordered = []
        try:
            view = getattr(self.spine, "current_view", None)
            names = list((view if view is not None else self.spine.value).get("name", []))
        except Exception:
            names = []
        for name in names:
            bucket = by_name.get(str(name), [])
            if bucket:
                ordered.append(bucket.pop(0))
        if ordered:
            return ordered
        out = []
        for kind, rid in self._spine_rows:
            if kind == "sample":
                s = self.store.sample_by_id(rid)
                if s is not None:
                    out.append(s)
        return out

    # ---- per-sample project_name ----------------------------------------
    def _set_sample_project(self, sample, value, *, refresh=False):
        """Persist a sample's project on ``Sample.md['project_name']`` (carried into its run md).

        Empty clears it (the experiment/project name is then the fallback at codegen time).
        """
        name = str(value or "").strip()
        self._update_sample_md(sample, "project_name", name or None)
        if refresh:
            self.refresh_spine()

    def _on_set_holder_project(self, _e):
        """Bulk-set the project_name of every sample on the selected sample's holder."""
        s = self._selected_sample()
        if s is None:
            _toast("select a sample on the holder you want to set", "warning")
            return
        name = (self.holder_project.value or "").strip()
        hid = s.holder_id
        if not hid:
            _toast("that sample is not on a holder", "warning")
            return
        members = self.store.list_samples(holder_id=hid)
        for m in members:
            self._set_sample_project(m, name)
        holder = self.store.holder_by_id(hid)
        self.refresh_spine()
        _toast("set project '{}' on {} sample(s) in holder '{}'".format(
            name or "(cleared)", len(members), holder.name if holder else "?"))

    def _on_adjust_positions(self, _e):
        samples = self._selected_samples()
        if not samples:
            _toast("select one or more samples to adjust", "warning")
            return
        if any(s.refined is not None for s in samples) and not self.adjust_clear_refined.value:
            _toast("selected sample(s) have refined positions; nominal will change but refined will remain", "warning")
        n = self.store.adjust_nominal_axis(
            [s.id for s in samples], self.adjust_axis.value, float(self.adjust_value.value),
            mode=self.adjust_mode.value, clear_refined=bool(self.adjust_clear_refined.value))
        self.refresh_spine()
        _toast("adjusted nominal {} on {} sample(s)".format(self.adjust_axis.value, n))

    def _on_remove_holder(self, *, delete_samples=False):
        hid = self.move_holder.value
        if not hid:
            s = self._selected_sample()
            hid = s.holder_id if s is not None else None
        if not hid:
            _toast("pick a holder or select a sample on one", "warning")
            return
        holder = self.store.holder_by_id(hid)
        name = holder.name if holder is not None else "?"
        members = self.store.list_samples(holder_id=hid)
        deleted = self.store.delete_holder(hid, delete_samples=delete_samples)
        self.refresh_spine()
        action = "removed holder '{}'".format(name)
        if delete_samples:
            action += " and deleted {} sample(s)".format(deleted)
        else:
            action += "; detached {} sample(s)".format(len(members))
        _toast(action)

    def _on_clear_holders(self):
        count = len(self.store.list_holders())
        self.store.clear_holders(delete_samples=False)
        self.refresh_spine()
        _toast("cleared {} holder(s); samples were detached".format(count))

    def _edit_reference(self, ref_id, col, val):
        """Apply an inline edit to a reference row (only its name is editable here)."""
        r = next((r for r in self.project.references if r.id == ref_id), None)
        if r is None:
            return
        if col == "name":
            r.name = str(val).strip() or r.name

    def _set_sample_scan(self, name, enabled):
        """Toggle one sample's scan-target membership in the microscope engine."""
        if self.micro is None or not name:
            return
        inter = self.micro.interactive
        try:
            current = {b.name for b in inter.get_scan_targets()}
        except Exception:
            current = set()
        if enabled:
            current.add(name)
        else:
            current.discard(name)
        try:
            inter.set_scan_selection(current)
        except Exception:
            pass


    def _on_add_blank(self, _e):
        self.store.add_sample(self._next_name())
        self.refresh_spine()

    def _on_remove_selected(self, _e):
        rows = self._selected_rows()
        if not rows:
            _toast("select one or more rows (checkboxes / ctrl+click)", "warning")
            return
        n_samples = n_refs = 0
        ref_ids = {rid for kind, rid in rows if kind == "ref"}
        for kind, rid in rows:
            if kind == "sample":
                self.store.delete_sample(rid)
                n_samples += 1
        if ref_ids:
            self.project.references = [
                r for r in self.project.references if r.id not in ref_ids]
            n_refs = len(ref_ids)
        self.spine.selection = []
        self.refresh_spine()
        bits = []
        if n_samples:
            bits.append("{} sample(s)".format(n_samples))
        if n_refs:
            bits.append("{} reference(s)".format(n_refs))
        _toast("removed " + " + ".join(bits))

    def _on_set_active(self, _e):
        s = self._selected_sample()
        if s is None:
            _toast("select a sample row in the sidebar first", "warning")
            return
        self.store.set_active_sample(s.id)
        self.refresh_spine()
        _toast("active sample set to intent — load '{}' from beamline session".format(s.name))

    def _on_goto_selected(self, _e):
        """Move the stage to the selected master-list row (sample or reference)."""
        if self.micro is None:
            _toast("open the Align & Samples tab first (the microscope must be running)",
                   "warning")
            return
        rows = self._selected_rows()
        if not rows:
            _toast("select a row to move to", "warning")
            return
        kind, rid = rows[0]
        if kind == "sample":
            s = self.store.sample_by_id(rid)
            xyz = self._sample_xyz(s) if s is not None else None
            label = s.name if s is not None else "?"
        else:
            r = next((r for r in self.project.references if r.id == rid), None)
            xyz = (r.x or 0.0, r.y or 0.0, r.z or 0.0) if r is not None else None
            label = r.name if r is not None else "?"
        if xyz is None:
            _toast("'{}' has no recorded position to move to".format(label), "warning")
            return
        self.micro.interactive.goto_xyz(*xyz)
        _toast("moving to '{}'".format(label))

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
        samples = self._selected_samples()
        hid = self.move_holder.value
        if not samples or not hid:
            _toast("select sample(s) and a holder", "warning")
            return
        for s in samples:
            self.store.set_sample_holder(s.id, hid)
        self.refresh_spine()
        _toast("moved {} sample(s)".format(len(samples)))

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
        assign_btn = pn.widgets.Button(name="assign → selected sample", width=180)
        assign_btn.on_click(self._on_assign_here)
        ref_btn = pn.widgets.Button(name="+ reference here", width=160)
        ref_btn.on_click(self._on_ref_here)
        sync_btn = pn.widgets.Button(name="↻ markers from samples", width=180)
        sync_btn.on_click(lambda _e: self.sync_markers())

        # Capture-position controls. These are folded into the microscope's **Move** tab once
        # the microscope is built (see _ensure_microscope). Every "sample" they refer to is a
        # row in the one master **Sample list** in the sidebar — there is no second list.
        self.capture_controls = pn.Column(
            pn.pane.Markdown("### Capture position\nMove with the image, then:"),
            self.interlock_banner,
            self.pos_readout,
            self.capture_name,
            self.capture_holder,
            pn.Row(new_btn, assign_btn),
            pn.Row(ref_btn, sync_btn),
            pn.pane.Markdown(
                "<span style='color:#777;font-size:12px'><b>new sample here</b> adds a row to the "
                "Sample list at the current position; <b>assign → selected sample</b> writes this "
                "position onto the row selected in that list. Positioned samples show as lime "
                "markers, references as yellow. Use the <b>Scan</b> tabs for line/grid/polygon "
                "alignment.</span>"),
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
            # Fold the capture-position controls into the microscope's Move tab. The single
            # sample list is the sidebar master list; the engine renders markers headlessly.
            ui.capture_slot.append(self.capture_controls)
            ui.attach_periodic_callbacks()
            pn.state.add_periodic_callback(self._refresh_pos, period=500)
            # Poll the RE-busy interlock at ~1.5 Hz (uncached; the flag has a 30s TTL).
            pn.state.add_periodic_callback(self._refresh_interlock, period=650)
            # Refresh the read-only proposal occasionally (changes when staff set a new proposal).
            pn.state.add_periodic_callback(self._refresh_proposal_status, period=5000)
            # Treat the Redis/offline sample store as two-way; reflect external edits promptly.
            pn.state.add_periodic_callback(self._poll_store_refresh, period=2000)
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
        col.append(self._wiz_naming_card())

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

    def _wiz_naming_card(self):
        st = self.wizard
        spec = dict(st.name_spec or {})
        prefix = pn.widgets.TextInput(name="prefix", value=str(spec.get("name_prefix", "")), width=160)
        include_energy = pn.widgets.Checkbox(name="energy", value=bool(spec.get("include_energy", True)))
        include_arc = pn.widgets.Checkbox(name="WAXS arc", value=bool(spec.get("include_arc", True)))
        include_incidence = pn.widgets.Checkbox(name="incidence", value=bool(spec.get("include_incidence", False)))
        arc_fmt = pn.widgets.TextInput(name="arc format", value=str(spec.get("arc_fmt", "wa{:04.1f}")), width=130)
        extra = pn.widgets.TextInput(name="extra tokens", value=" ".join(spec.get("extra_tokens", []) or []),
                                     placeholder="px_{piezo_x:.1f} py_{piezo_y:.1f}", width=320)
        preview = pn.pane.Markdown("")

        def _apply(_e=None):
            toks = [t for t in str(extra.value or "").split() if t]
            ns = {
                "name_prefix": prefix.value.strip(),
                "include_energy": bool(include_energy.value),
                "include_arc": bool(include_arc.value),
                "include_incidence": bool(include_incidence.value),
                "arc_fmt": arc_fmt.value or "wa{:04.1f}",
            }
            if toks:
                ns["extra_tokens"] = toks
            # Drop defaults/empty prefix to keep generated scripts thin.
            if not ns["name_prefix"]:
                ns.pop("name_prefix")
            st.name_spec = ns
            preview.object = self._naming_preview(ns)

        for w in (prefix, include_energy, include_arc, include_incidence, arc_fmt, extra):
            w.param.watch(_apply, "value")
        _apply()
        return pn.Card(
            pn.pane.Markdown("Use recorded data-key tokens only. Common tokens: `{energy_energy}`, "
                             "`{waxs_arc}`, `{incident_angle}`, `{piezo_x}`, `{piezo_y}`, `{stage_phi}`."),
            pn.Row(prefix, arc_fmt),
            pn.Row(include_energy, include_arc, include_incidence),
            extra,
            preview,
            title="Advanced naming helper", collapsed=True, sizing_mode="stretch_width")

    @staticmethod
    def _naming_preview(name_spec):
        try:
            from smi_plans import preview_bar_name
            template = preview_bar_name("sample", name_spec=name_spec, printer=None)
            return "template: `{}`".format(template)
        except Exception as exc:
            return "preview unavailable: `{}`".format(exc)

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
        elif ax.type == "energy":
            # the energy grid gets the visual boundaries+density editor; settle/flux_reseek
            # remain simple form fields below it
            fields.append(self._energy_fields(i, ax))
            for f in axis_param_schema(ax.type):
                fields.append(self._param_widget(ax, f, None, self._render_wizard))
        elif ax.type == "temperature":
            fields.append(self._temperature_fields(i, ax))
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
        return pn.Column(
            pn.Row(pn.pane.HTML("<b>Incident angles</b>"), sel), body,
            self._named_list_row(i, ax, "incidence", save_fn=self._incidence_save_list,
                                 open_fn=self._incidence_open_list, label="angle"))

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
            st.axes[i].params.pop("list_name", None)   # edited -> detach from any saved list
        self._render_wizard()

    def _incidence_set_list(self, i, text):
        st = self.wizard
        if 0 <= i < len(st.axes):
            st.axes[i].params["values"] = _parse_floatlist(text)
            st.axes[i].params.pop("range", None)
            st.axes[i].params.pop("list_name", None)   # edited -> detach from any saved list
        self._render_wizard()

    def _incidence_save_list(self, i, name):
        """Persist the current incident-angle set as a NamedList(kind='incidence') in db=2."""
        name = (name or "").strip()
        st = self.wizard
        if not (0 <= i < len(st.axes)):
            return
        if not name:
            _toast("type a name for the angle list first", "warning")
            return
        if self.listdb is None:
            _toast("no list store connected", "error")
            return
        ax = st.axes[i]
        values = list(ax.values())
        rng = ax.params.get("range")
        spec = {"range": [float(v) for v in rng]} if (rng and len(rng) == 3) else \
               {"values": list(values)}
        try:
            self.listdb.save_list(name, "incidence", values, spec=spec, units="deg")
        except Exception as exc:  # noqa: BLE001
            _toast("save failed: {}".format(exc), "error")
            return
        ax.params["list_name"] = name
        self._render_wizard()
        _toast("saved angle list '{}' ({} angles)".format(name, len(values)))

    def _incidence_open_list(self, i, name):
        """Load a stored incidence NamedList back into the editor (range or explicit values)."""
        if not name or name == "(load saved…)":
            return
        st = self.wizard
        if not (0 <= i < len(st.axes)) or self.listdb is None:
            return
        nl = self.listdb.get_list(name, "incidence")
        if nl is None:
            _toast("angle list '{}' not found".format(name), "warning")
            return
        ax = st.axes[i]
        spec = nl.spec or {}
        if spec.get("range") and len(spec["range"]) == 3:
            ax.params["range"] = [float(v) for v in spec["range"]]
            ax.params.pop("values", None)
        else:
            ax.params["values"] = [float(v) for v in (spec.get("values") or nl.values or [])]
            ax.params.pop("range", None)
        ax.params["list_name"] = name
        self._render_wizard()
        _toast("opened angle list '{}'".format(name))

    # ---- temperature: setpoints + ramp/hold/cycle editor ----------------
    @staticmethod
    def _temperature_mode(ax):
        """'ramp' if the axis carries a [start,stop,step] range, else 'list'."""
        rng = ax.params.get("range")
        return "ramp" if (rng and len(rng) == 3) else "list"

    def _temperature_fields(self, i, ax):
        """A graphical temperature editor: setpoints (explicit list OR a start/stop/step ramp),
        an anneal-then-cool ``cycle`` toggle, per-setpoint hold (soak) + first-point soak, and an
        advisory ramp rate (stored for the user; the heater owns the actual rate). The materialized
        setpoint list + this recipe persist as a reusable ``NamedList(kind="temperature")``."""
        mode = self._temperature_mode(ax)
        sel = pn.widgets.RadioButtonGroup(
            name="Setpoints", options=["list", "ramp"], value=mode, width=160)
        sel.param.watch(lambda e, idx=i: self._temperature_set_mode(idx, e.new), "value")
        body = pn.Column()
        if mode == "ramp":
            rng = list(ax.params.get("range") or [30.0, 120.0, 10.0])
            start = pn.widgets.FloatInput(name="start (°C)", value=float(rng[0]), step=1.0, width=110)
            stop = pn.widgets.FloatInput(name="stop (°C)", value=float(rng[1]), step=1.0, width=110)
            step = pn.widgets.FloatInput(name="step (°C)", value=float(rng[2]), step=1.0, width=110)

            def _apply(_e, idx=i, s=start, e2=stop, st=step):
                self._temperature_set_range(idx, s.value, e2.value, st.value)
            for w in (start, stop, step):
                w.param.watch(_apply, "value")
            body.append(pn.Row(start, stop, step))
        else:
            vals = pn.widgets.TextInput(
                name="Setpoints (°C)", value=_fmt_floatlist(ax.params.get("values") or []),
                width=340)
            vals.param.watch(lambda e, idx=i: self._temperature_set_list(idx, e.new), "value")
            body.append(vals)

        cycle = pn.widgets.Checkbox(
            name="↩ cycle (anneal then cool: up to the top, back to the start — doubles the scan)",
            value=bool(ax.params.get("cycle")))
        cycle.param.watch(lambda e, idx=i: self._temperature_set_flag(idx, "cycle", e.new), "value")

        soak = pn.widgets.FloatInput(name="hold at each (s)", value=float(ax.params.get("soak", 120.0)),
                                     step=10.0, start=0.0, width=140)
        soak.param.watch(lambda e, idx=i: self._temperature_set_param(idx, "soak", e.new), "value")
        first = pn.widgets.FloatInput(name="first-point hold (s)",
                                      value=float(ax.params.get("first_soak", 300.0)),
                                      step=10.0, start=0.0, width=150)
        first.param.watch(lambda e, idx=i: self._temperature_set_param(idx, "first_soak", e.new),
                          "value")
        # Advisory ramp rate (heater owns the real rate; we keep it as md for the user + future use).
        ramp = pn.widgets.FloatInput(name="ramp rate (°C/min, advisory)",
                                     value=float(ax.params.get("ramp_rate", 10.0)),
                                     step=1.0, start=0.0, width=190)
        ramp.param.watch(lambda e, idx=i: self._temperature_set_param(idx, "ramp_rate", e.new),
                         "value")

        pts = ax.values()
        total = len(pts)
        cyc = " · ↑↓ cycle" if ax.params.get("cycle") else ""
        count = pn.pane.HTML(
            "<b>{} setpoint(s)</b>{} &nbsp;<span style='color:#777'>{}</span>".format(
                total, cyc, ", ".join("{:g}".format(v) for v in pts[:12])
                + (" …" if total > 12 else "")))
        return pn.Column(
            pn.Row(pn.pane.HTML("<b>Temperature</b>"), sel), body, count,
            pn.Row(soak, first), pn.Row(ramp), cycle,
            self._named_list_row(i, ax, "temperature", save_fn=self._temperature_save_list,
                                 open_fn=self._temperature_open_list, label="temperature"))

    def _temperature_set_mode(self, i, mode):
        st = self.wizard
        if not (0 <= i < len(st.axes)):
            return
        ax = st.axes[i]
        if mode == "ramp":
            cur = ax.values()
            if len(cur) >= 2:
                start, stop = cur[0], cur[-1]
                step = round((stop - start) / (len(cur) - 1), 4) or 10.0
            else:
                start, stop, step = 30.0, 120.0, 10.0
            ax.params["range"] = [start, stop, step]
            ax.params.pop("values", None)
        else:
            ax.params["values"] = ax.values()
            ax.params.pop("range", None)
        ax.params.pop("list_name", None)
        self._render_wizard()

    def _temperature_set_range(self, i, start, stop, step):
        st = self.wizard
        if 0 <= i < len(st.axes):
            st.axes[i].params["range"] = [float(start), float(stop), float(step)]
            st.axes[i].params.pop("values", None)
            st.axes[i].params.pop("list_name", None)
        self._render_wizard()

    def _temperature_set_list(self, i, text):
        st = self.wizard
        if 0 <= i < len(st.axes):
            st.axes[i].params["values"] = _parse_floatlist(text)
            st.axes[i].params.pop("range", None)
            st.axes[i].params.pop("list_name", None)
        self._render_wizard()

    def _temperature_set_flag(self, i, key, on):
        st = self.wizard
        if 0 <= i < len(st.axes):
            if on:
                st.axes[i].params[key] = True
            else:
                st.axes[i].params.pop(key, None)
            st.axes[i].params.pop("list_name", None)
        self._render_wizard()

    def _temperature_set_param(self, i, key, value):
        st = self.wizard
        if 0 <= i < len(st.axes):
            try:
                st.axes[i].params[key] = float(value)
            except (TypeError, ValueError):
                return
            # soak/first_soak/ramp_rate are run params, not the value list -> don't detach the name
        self._render_wizard()

    def _temperature_save_list(self, i, name):
        """Persist the current setpoints as a NamedList(kind='temperature').

        Stores the materialized setpoint ``values`` (post-cycle, what the plan resolves), an
        editable ``spec`` (``{values|range, cycle}``), and ``md`` extras (ramp_rate, soak,
        first_soak) used for nice interactions / future backend re-materialization.
        """
        name = (name or "").strip()
        st = self.wizard
        if not (0 <= i < len(st.axes)):
            return
        if not name:
            _toast("type a name for the temperature list first", "warning")
            return
        if self.listdb is None:
            _toast("no list store connected", "error")
            return
        ax = st.axes[i]
        values = list(ax.values())
        rng = ax.params.get("range")
        spec = {"cycle": bool(ax.params.get("cycle"))}
        if rng and len(rng) == 3:
            spec["range"] = [float(v) for v in rng]
        else:
            spec["values"] = [float(v) for v in (ax.params.get("values") or [])]
        md = {k: float(ax.params[k]) for k in ("ramp_rate", "soak", "first_soak")
              if ax.params.get(k) is not None}
        try:
            self.listdb.save_list(name, "temperature", values, spec=spec, units="C", md=md)
        except Exception as exc:  # noqa: BLE001
            _toast("save failed: {}".format(exc), "error")
            return
        ax.params["list_name"] = name
        self._render_wizard()
        _toast("saved temperature list '{}' ({} setpoints)".format(name, len(values)))

    def _temperature_open_list(self, i, name):
        """Load a stored temperature NamedList back into the editor (setpoints + ramp/hold/cycle)."""
        if not name or name == "(load saved…)":
            return
        st = self.wizard
        if not (0 <= i < len(st.axes)) or self.listdb is None:
            return
        nl = self.listdb.get_list(name, "temperature")
        if nl is None:
            _toast("temperature list '{}' not found".format(name), "warning")
            return
        ax = st.axes[i]
        spec = nl.spec or {}
        if spec.get("range") and len(spec["range"]) == 3:
            ax.params["range"] = [float(v) for v in spec["range"]]
            ax.params.pop("values", None)
        else:
            ax.params["values"] = [float(v) for v in (spec.get("values") or nl.values or [])]
            ax.params.pop("range", None)
        if spec.get("cycle"):
            ax.params["cycle"] = True
        else:
            ax.params.pop("cycle", None)
        for k in ("ramp_rate", "soak", "first_soak"):
            if (nl.md or {}).get(k) is not None:
                ax.params[k] = float(nl.md[k])
        ax.params["list_name"] = name
        self._render_wizard()
        _toast("opened temperature list '{}'".format(name))

    # ---- energy: visual boundaries+density editor --------------------
    @staticmethod
    def _energy_grid(ax):
        """The {'boundaries':[...], 'steps':[...]} grid (seeded if absent)."""
        g = ax.params.get("grid")
        if not isinstance(g, dict) or "boundaries" not in g:
            g = {"boundaries": [2470.0, 2472.0, 2476.0, 2530.0], "steps": [1.0, 0.25, 5.0]}
            ax.params["grid"] = g
        # keep steps length = len(boundaries)-1
        b, s = g["boundaries"], g.get("steps", [])
        while len(s) < max(0, len(b) - 1):
            s.append(1.0)
        g["steps"] = s[:max(0, len(b) - 1)]
        return g

    def _energy_fields(self, i, ax):
        from smi_acquire.spec import energy_grid_values
        g = self._energy_grid(ax)
        bounds = [float(x) for x in g["boundaries"]]
        steps = [float(x) for x in g["steps"]]
        pts = energy_grid_values(g)

        rows = pn.Column()
        # one row per REGION: [start] -> [stop]  step [s]   (boundaries shared between regions)
        for r in range(len(bounds) - 1):
            b_lo = pn.widgets.FloatInput(value=bounds[r], width=92, name=("from (eV)" if r == 0 else ""))
            b_hi = pn.widgets.FloatInput(value=bounds[r + 1], width=92, name=("to (eV)" if r == 0 else ""))
            step = pn.widgets.FloatInput(value=steps[r], width=78, step=0.05, start=0.0,
                                         name=("step" if r == 0 else ""))
            rm = pn.widgets.Button(name="✕", button_type="danger", width=34,
                                   disabled=(len(bounds) <= 2))
            colr = (registry.AXIS_KIND_BY_TYPE.get("energy").color
                    if registry.AXIS_KIND_BY_TYPE.get("energy") else "#7E57C2")
            tag = pn.pane.HTML("<span style='display:inline-block;width:10px;height:10px;"
                               "border-radius:50%;background:{}'></span>".format(
                                   _region_color(colr, r)))

            def _apply(_e, idx=i, reg=r, lo=b_lo, hi=b_hi, sp=step):
                self._energy_set_region(idx, reg, lo.value, hi.value, sp.value)
            for w in (b_lo, b_hi, step):
                w.param.watch(_apply, "value")
            rm.on_click(lambda _e, idx=i, reg=r: self._energy_remove_boundary(idx, reg))
            rows.append(pn.Row(tag, b_lo, pn.pane.HTML("→"), b_hi, step, rm))

        add = pn.widgets.Button(name="+ region (split last)", width=170, button_type="primary")
        add.on_click(lambda _e, idx=i: self._energy_add_region(idx))

        updown = pn.widgets.Checkbox(
            name="↩ cycle down (there-and-back: up then back to the start, doubles the scan)",
            value=bool(ax.params.get("updown")))
        updown.param.watch(lambda e, idx=i: self._energy_set_updown(idx, e.new), "value")

        plot = self._energy_plot(i, ax, pts, bounds)
        total = len(pts) * 2 if ax.params.get("updown") else len(pts)
        cyc = " · ↑↓ up+down" if ax.params.get("updown") else ""
        count = pn.pane.HTML(
            "<b>{} energy points</b>{} &nbsp; <span style='color:#777'>{:g}–{:g} eV</span>".format(
                total, cyc, pts[0] if pts else 0, pts[-1] if pts else 0))
        return pn.Column(
            pn.pane.HTML("<b>Energy regions</b> &nbsp;<span style='color:#777;font-size:12px'>"
                         "boundaries + a step (density) per region — drag the boundary dots on the "
                         "plot, or edit below</span>"),
            plot, count, rows, add, updown,
            self._energy_named_list_row(i, ax))

    def _named_list_row(self, i, ax, kind, *, save_fn, open_fn, label):
        """Reusable 'name this list / open a saved one' controls for a list-bearing axis.

        Shared by the per-kind editors (energy / incidence / temperature). ``save_fn(idx, name)``
        persists the current axis as a ``NamedList(kind=…)``; ``open_fn(idx, name)`` loads one back.
        When ``ax.params['list_name']`` is set, codegen references it by name via ``resolve_list``.
        """
        name_in = pn.widgets.TextInput(
            placeholder="name this {} list…".format(label), width=200,
            value=str(ax.params.get("list_name") or ""))
        save = pn.widgets.Button(name="⤓ save list", width=110, button_type="primary",
                                 disabled=self.listdb is None)
        save.on_click(lambda _e, idx=i, w=name_in: save_fn(idx, w.value))
        opts = ["(load saved…)"] + (self.listdb.list_names(kind) if self.listdb else [])
        open_sel = pn.widgets.Select(options=opts, width=180, value="(load saved…)")
        open_sel.param.watch(lambda e, idx=i: open_fn(idx, e.new), "value")
        status = ""
        if ax.params.get("list_name"):
            status = ("<span style='color:#2e7d32;font-size:12px'>↳ generates "
                      "<code>resolve_list(\"{}\", kind=\"{}\")</code></span>".format(
                          ax.params["list_name"], kind))
        loc = ("" if self.listdb is None
               else "<span style='color:#777;font-size:11px'> · {}</span>".format(
                   self.listdb.location))
        return pn.Column(
            pn.pane.HTML("<b>Reusable list</b> "
                         "<span style='color:#777;font-size:12px'>save to reference by name "
                         "(no copy-paste); open to edit a stored one</span>" + loc),
            pn.Row(name_in, save, open_sel),
            pn.pane.HTML(status) if status else pn.pane.HTML(""))

    def _energy_named_list_row(self, i, ax):
        """Name / save / open controls for the energy region set (a ``NamedList(kind="energy")``).

        Saving stores the authoritative materialized ``values`` (what the plan resolves), the
        ``{boundaries, steps, updown}`` ``spec`` (so the graphical editor can re-open it), and
        ``md`` extras (flux re-seek). See :meth:`_named_list_row`.
        """
        return self._named_list_row(i, ax, "energy", save_fn=self._energy_save_list,
                                    open_fn=self._energy_open_list, label="energy")

    def _energy_plot(self, i, ax, pts, bounds):
        """A Bokeh preview: energy points as dots + draggable boundary markers (write back)."""
        try:
            from bokeh.plotting import figure
            from bokeh.models import ColumnDataSource, PointDrawTool, Span
        except Exception:
            return pn.pane.HTML("<i>(install bokeh for the visual editor)</i>")
        colr = (registry.AXIS_KIND_BY_TYPE.get("energy").color
                if registry.AXIS_KIND_BY_TYPE.get("energy") else "#7E57C2")
        fig = figure(height=150, sizing_mode="stretch_width", toolbar_location=None,
                     tools="", x_axis_label="energy (eV)", y_range=(-0.6, 1.2))
        fig.yaxis.visible = False
        fig.ygrid.visible = False
        # the scan points (colored by region)
        if pts:
            xs, cs = [], []
            for p in pts:
                r = 0
                for bi in range(len(bounds) - 1):
                    if bounds[bi] <= p < bounds[bi + 1] or bi == len(bounds) - 2:
                        r = bi
                        break
                xs.append(p)
                cs.append(_region_color(colr, r))
            fig.scatter(xs, [0] * len(xs), size=7, color=cs, alpha=0.85)
        # boundary spans
        for b in bounds:
            fig.add_layout(Span(location=b, dimension="height", line_color="#444",
                                line_dash="dashed", line_width=1))
        # draggable boundary markers (y=0.7); dragging x writes back to the model
        bsrc = ColumnDataSource(data={"x": list(bounds), "y": [0.7] * len(bounds)})
        rend = fig.scatter("x", "y", source=bsrc, size=14, color=colr, marker="triangle",
                           line_color="#222")
        draw = PointDrawTool(renderers=[rend], add=False, drag=True)
        fig.add_tools(draw)
        fig.toolbar.active_tap = draw

        def _on_drag(attr, old, new, idx=i):
            xs = sorted(float(v) for v in (new.get("x") or []))
            if len(xs) >= 2:
                self._energy_set_boundaries(idx, xs)
        bsrc.on_change("data", _on_drag)
        return pn.pane.Bokeh(fig, sizing_mode="stretch_width")

    def _energy_dirty(self, i):
        """Editing the regions detaches the axis from any saved list name.

        The generated script must always reflect exactly what's on screen; once the user changes
        boundaries/steps/updown, the stored list no longer matches, so we drop ``list_name`` (the
        codegen then emits the literal list until the user re-saves under a name).
        """
        st = self.wizard
        if 0 <= i < len(st.axes):
            st.axes[i].params.pop("list_name", None)

    def _energy_set_region(self, i, reg, lo, hi, step):
        st = self.wizard
        if not (0 <= i < len(st.axes)):
            return
        g = self._energy_grid(st.axes[i])
        b, s = g["boundaries"], g["steps"]
        if 0 <= reg < len(b) - 1:
            b[reg] = float(lo)
            b[reg + 1] = float(hi)
            s[reg] = float(step)
            # keep boundaries monotonic (a shared boundary moves both neighbors)
            g["boundaries"] = b
        self._energy_dirty(i)
        self._render_wizard()

    def _energy_set_updown(self, i, on):
        st = self.wizard
        if 0 <= i < len(st.axes):
            if on:
                st.axes[i].params["updown"] = True
            else:
                st.axes[i].params.pop("updown", None)
        self._energy_dirty(i)
        self._render_wizard()

    def _energy_set_boundaries(self, i, xs):
        st = self.wizard
        if not (0 <= i < len(st.axes)):
            return
        g = self._energy_grid(st.axes[i])
        old = g["boundaries"]
        # preserve the per-region steps as best we can (same count if unchanged)
        new_b = sorted(float(x) for x in xs)
        if len(new_b) == len(old):
            g["boundaries"] = new_b
        else:
            g["boundaries"] = new_b
            g["steps"] = (g["steps"] + [1.0] * len(new_b))[:max(0, len(new_b) - 1)]
        self._energy_dirty(i)
        self._render_wizard()

    def _energy_add_region(self, i):
        st = self.wizard
        if not (0 <= i < len(st.axes)):
            return
        g = self._energy_grid(st.axes[i])
        b, s = g["boundaries"], g["steps"]
        # split the last region at its midpoint with the same step
        lo, hi = b[-2], b[-1]
        mid = round((lo + hi) / 2, 4)
        b.insert(len(b) - 1, mid)
        s.append(s[-1] if s else 1.0)
        self._energy_dirty(i)
        self._render_wizard()

    def _energy_remove_boundary(self, i, reg):
        st = self.wizard
        if not (0 <= i < len(st.axes)):
            return
        g = self._energy_grid(st.axes[i])
        b, s = g["boundaries"], g["steps"]
        if len(b) > 2:
            # removing region `reg` drops its upper boundary (merging into the next region)
            drop = min(reg + 1, len(b) - 1)
            b.pop(drop)
            if reg < len(s):
                s.pop(reg)
        self._energy_dirty(i)
        self._render_wizard()

    # ---- energy: reusable NamedList (save / open) -----------------------
    def _energy_save_list(self, i, name):
        """Persist the current energy region set as a NamedList(kind='energy') in db=2.

        Stores authoritative ``values`` (the eV points the plan resolves), the editable ``spec``
        (``{boundaries, steps, updown}`` — re-openable in this graphical editor) and ``md`` extras
        (flux re-seek) used only for nice interactions. Tags the axis with ``list_name`` so codegen
        emits ``resolve_list(name, kind="energy")`` instead of the literal list.
        """
        from smi_acquire.spec import energy_grid_values
        name = (name or "").strip()
        st = self.wizard
        if not (0 <= i < len(st.axes)):
            return
        if not name:
            _toast("type a name for the energy list first", "warning")
            return
        if self.listdb is None:
            _toast("no list store connected", "error")
            return
        ax = st.axes[i]
        g = self._energy_grid(ax)
        values = energy_grid_values(g)
        spec = {"boundaries": list(g["boundaries"]), "steps": list(g["steps"]),
                "updown": bool(ax.params.get("updown"))}
        md = {}
        if ax.params.get("flux_reseek"):
            md["flux_reseek"] = True
        try:
            self.listdb.save_list(name, "energy", values, spec=spec, units="eV", md=md)
        except Exception as exc:  # noqa: BLE001
            _toast("save failed: {}".format(exc), "error")
            return
        ax.params["list_name"] = name
        self._render_wizard()
        _toast("saved energy list '{}' ({} points)".format(name, len(values)))

    def _energy_open_list(self, i, name):
        """Load a stored energy NamedList back into the editor (restores boundaries/steps/updown)."""
        if not name or name == "(load saved…)":
            return
        st = self.wizard
        if not (0 <= i < len(st.axes)) or self.listdb is None:
            return
        nl = self.listdb.get_list(name, "energy")
        if nl is None:
            _toast("energy list '{}' not found".format(name), "warning")
            return
        ax = st.axes[i]
        spec = nl.spec or {}
        if "boundaries" in spec and "steps" in spec:
            ax.params["grid"] = {"boundaries": [float(x) for x in spec["boundaries"]],
                                 "steps": [float(x) for x in spec["steps"]]}
        elif nl.values:
            # spec-less entry: rebuild a single fine region spanning the stored values
            vals = [float(v) for v in nl.values]
            ax.params["grid"] = {"boundaries": [vals[0], vals[-1]],
                                 "steps": [vals[1] - vals[0] if len(vals) > 1 else 1.0]}
        if spec.get("updown"):
            ax.params["updown"] = True
        else:
            ax.params.pop("updown", None)
        if (nl.md or {}).get("flux_reseek"):
            ax.params["flux_reseek"] = True
        ax.params["list_name"] = name
        self._render_wizard()
        _toast("opened energy list '{}'".format(name))

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
        elif ax.type == "energy":
            fields.append(self._energy_fields(i, ax))
            for f in axis_param_schema(ax.type):
                fields.append(self._param_widget(ax, f, None, self._render_wizard))
        elif ax.type == "temperature":
            fields.append(self._temperature_fields(i, ax))
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
            title="SMI-SWAXS Acquire",
            accent_base_color=ACCENT, header_background=ACCENT,
            sidebar=[self.spine_panel],
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
