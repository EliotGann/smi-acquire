# Design notes — smi-acquire GUI options

This document records the framework options considered and the reasoning behind the candidate
layouts, so we can choose a direction deliberately.

---

## 1. The governing idea: a headless core

Whatever the front-end, the job decomposes into four pure-data concerns:

1. **Sample model** — edit a `SampleList` flexibly (paste, CSV, manual, future visual bar).
2. **Guidance** — map "what are you doing?" onto the A–O technique archetypes.
3. **Parameter capture** — a typed form per technique.
4. **Output** — emit a runnable script *now*; submit a queueserver item *later*.

None of these require a GUI. So `smi_acquire` is a **framework-agnostic, unit-tested core**,
and each GUI is a thin binding. This is the single most important design decision: it makes
the framework choice reversible and the queueserver migration localized.

It also reuses, rather than re-implements, the `smi_plans._samples` model — the library authors
deliberately kept that half pure-Python *"so a GUI could eventually import the same building
blocks"* (their README). We take them up on it: one source of truth, no drift.

### Why a *registry* instead of bespoke forms

`techniques.py` describes each archetype as data (`ParamSpec` list + a call template). A
front-end builds widgets by **iterating the registry**, and `codegen` renders calls by
**formatting the template** with rendered literals. Adding a technique or a knob is a data
edit, not new UI code in four apps. The test suite compiles every generated A–O script, so a
malformed template fails CI rather than the beamline.

---

## 2. Framework options

### Option A — Panel/Bokeh  ✅ recommended primary

- **Pros:** matches your existing stack exactly (`smi-browser`, `samples/locate-samples`),
  already in your pixi envs; `Tabulator` is an excellent paste-friendly sample grid;
  `CodeEditor` gives syntax-highlighted output; trivial to embed alongside the analysis app
  later (one server, multiple pages); served over the web → works on the beamline network with
  no client install.
- **Cons:** reactive wiring is more verbose than NiceGUI; very custom layouts fight the
  templates a bit.
- **Verdict:** lowest friction to something real and shippable here.

### Option B — Qt (PySide6) desktop

- **Pros:** native widgets, the most flexible tables/dialogs/keyboard handling, no browser,
  feels like an instrument-control app; good if this becomes a always-open console tool.
- **Cons:** new heavy dependency (not installed); packaging/remote-display friction on
  beamline Linux (X forwarding / VNC); more boilerplate.
- **Verdict:** keep as a candidate; strongest if the tool wants to live *beside* the
  RunEngine console rather than in a browser. The mockup proves the core ports cleanly.

### Option C — NiceGUI

- **Pros:** the most compact reactive Python API of the web options; very pleasant for
  form-heavy tools; ag-Grid built in.
- **Cons:** smaller ecosystem than Panel; another dependency; less synergy with your existing
  Panel analysis app.
- **Verdict:** good ergonomics benchmark; include to compare against Panel's verbosity.

### Option D — Streamlit (considered, not built)

- Rerun-on-every-interaction model is awkward for a stateful multi-step builder with a live
  table; would fight the wizard. Omitted in favor of NiceGUI as the "lightweight web" sample.

### Recommendation

Build on **Panel** for the real tool (reuses your stack, web-deployable, pairs with the
browser app). Keep the **Qt** and **NiceGUI** mockups as living proof the core is portable, so
the decision stays open and cheap to revisit.

---

## 3. Layout candidates

Two distinct *interaction philosophies*, each rendered in Panel; the Qt/NiceGUI apps echo the
dashboard so all four are comparable.

### Candidate 1 — Guided wizard (`panel_wizard.py`)

```
┌── Steps ─────┐   ┌──────────────────────────────────────────────┐
│ > 1 Samples  │   │  Build your sample bar                        │
│   2 Goal     │   │  [ Tabulator: name | piezo_x | … | md ]       │
│   3 Technique│   │  [ Load CSV ]  [ Project name ]               │
│   4 Params   │   │                                               │
│   5 Script   │   │                          [ < Back ] [ Next > ]│
└──────────────┘   └──────────────────────────────────────────────┘
```

- One concern at a time; a goal questionnaire (`guidance`) auto-selects the technique and
  explains *why*.
- **For:** new/occasional users, training, minimizing "which of the 15 plans do I want?"
  paralysis.
- **Against:** slower for an expert who already knows they want technique B.

### Candidate 2 — Single-page dashboard (`panel_dashboard.py`)

```
┌ Sample bar ────────┐ ┌ Technique & params ─┐ ┌ Generated script ───────┐
│ [ Tabulator ]      │ │ I'm varying… ▾      │ │ status: OK – 2 samples  │
│ [+][-][Dup][CSV]   │ │ Technique ▾         │ │ ```python               │
│ Project name       │ │ summary text        │ │  …live script…          │
│                    │ │ ── params (cards) ──│ │ ```                     │
│                    │ │ exposure, grid, …   │ │ qserver item (preview)  │
└────────────────────┘ └─────────────────────┘ │ [ Download .py ]        │
                                                └─────────────────────────┘
```

- Everything visible; the script **regenerates on every change**; a "I'm varying…" filter
  reorders the technique list (lighter-weight guidance). Shows the **queueserver item** preview
  beside the script to make the future submit path tangible.
- **For:** power users / staff, fast iteration, demoing the qserver direction.
- **Against:** denser; assumes familiarity with the archetypes.

### Candidate 3 / 4 — Qt & NiceGUI

Both implement the dashboard philosophy (3-pane / 2-column) to demonstrate the core is
framework-independent and to compare native-desktop vs compact-web ergonomics.

---

## 4. Flexible sample-list handling (the core user value)

Implemented today via `samples.records_to_samples` / `samples_to_records`:

- **Paste / spreadsheet edit** (Tabulator / QTableWidget / ag-Grid).
- **CSV import** (maps to `Sample` fields; unknown columns → `md`).
- **Add / duplicate / delete** rows.
- **Tolerant parsing:** blanks → `None` (axis unused), `incident_angles` accepts space/comma/
  semicolon, `md` accepts JSON or free text.
- **Validation surfaced inline:** duplicate names raise and show in the status line, not a
  crash.

Natural next steps (not yet built, easy on this core):

- A **visual bar** widget (Bokeh scatter of piezo_x/piezo_y) to place/drag samples.
- **Templates** for common bar geometries (well plates, capillary racks).
- **Round-trip persistence** (`to_dicts`/`from_dicts` already exist) to save/restore a session.

---

## 5. The queueserver seam

`codegen.to_queueserver_item()` returns `{"name": <entry>, "kwargs": {"samples": [...],
...params}, "item_type": "plan"}` — the `BPlan` shape. The migration is:

- **today:** `generate_script()` → user copies text → pastes into IPython `RE(...)`.
- **next:** POST `to_queueserver_item()` to the qserver REST API; stream status back.

Because both share the same `(SampleList, technique, params)` inputs, the sample editor,
guidance, and forms are reused verbatim — only the "Output" pane changes.
