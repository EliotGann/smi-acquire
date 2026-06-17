# smi-acquire — design

## The thesis

An SMI-SWAXS experiment is not one of a fixed menu (the old A–O picker). It is an **assembly of
independent concerns**:

| concern | examples |
|---|---|
| beam / q-range | which detectors (+ the WAXS arc reach); what to record per event |
| apparatus / geometry | transmission vs grazing; alignment; heater; attenuators |
| sampling / scanning | a **stack** of nested scan axes (energy, temperature, incidence, spatial grid, potential, RH, time, manual) |
| manual / interactive | one-shot prompts that capture typed values into recorded Signals |
| samples | one run per sample; positions placed interactively |

So instead of "choose A–O", we **interrogate** the experimenter and *assemble* the plan they
need. The runnable embodiment of each concern already exists in
[`smi-plans`](../smi-plans) — `acquire`/`acquire_bar` wrapping `ScanAxis` builders — and the
interactive sample/microfocus tooling exists in
[`swaxs-beam-image`](../swaxs-beam-image). This package is the **interview + glue** between them.

## The pipeline

```
 interview.py        spec.py             codegen.py            dryrun.py
 (interrogation)  →  ExperimentSpec   →  smi_plans script  →   exec vs SimBeamline
   questions          (pure data)         (copy/paste)          → runs/events/warnings/errors
        │                  ▲
        └── seed ──────────┘     samples ← microscope/ (live camera + bookmarks → fake IOC)
```

### `spec.ExperimentSpec` — the contract in the middle

A JSON-serializable dataclass tree (`beam`, `apparatus`, `axes[]`, `manual_setup[]`, `samples`).
Design rules, all to keep the eventual queueserver path additive:

- **Device references are names/strings**, never live objects (`"waxs"`, `"piezo"`, `"att2_9"`).
  The generator maps names → bare identifiers; the sim provides stand-ins.
- **Axis order in `axes[]` = nesting order** (outermost first), mirroring
  `smi_plans._compose.acquire(axes=[...])`.
- It carries a `version` from day one.
- It computes its own analysis (event estimate, filename tokens, and the **same slow-outermost
  ordering guardrail** `_compose._check_axis_order` uses) so the GUI can warn pre-flight.

### `interview` — the interrogation

`INTAKE` is a small **branching question graph** (`Question` with a `when(answers)` predicate).
`seed_spec_from_intake(answers, sample_rows)` turns answers into a concrete starting spec with
axes pre-stacked **slow-outermost**. The user then refines each concern; `axis_param_schema`
provides the per-axis editor fields and `default_axis` the defaults. This replaces the old
`guidance` "which letter?" engine with "what shall I build for you?".

### `codegen` — spec → text

`render(spec)` emits idiomatic `smi_plans._compose` code (`acquire` for one sample,
`acquire_bar` for many). Because it is built from the composition layer, the generated script
**automatically obeys the SMI tenets** (one run/sample, recorded context, `{token}` filenames,
generators end-to-end, slow axes outermost). It emits only the imports it needs and renders the
*exact* expanded value lists (e.g. energy grids) so the script visits precisely the points the
spec counted — keeping the GUI estimate and the dry-run in lockstep.

### `dryrun` — validate without hardware

`dry_run(spec)` renders the script and `exec`s it in a namespace where `RE` just exhausts the
plan and counts messages, with the **`SimBeamline`** globals injected into the `smi_plans`
modules (vendored from `smi-plans/tests/conftest.py`). Running the *generated text* validates
the codegen too. Reports: number of runs (expect one per sample), primary events, ordering
warnings, and any exception with its type.

## Simulation: two fakes, one principle

- **`sim/fake_ioc.py`** — a caproto IOC publishing `SWAXS:SIM:` camera + X/Y/Z motor records.
  Drives the *interactive* microscope over EPICS (`pixi run dev-ioc`). Vendored from
  swaxs-beam-image.
- **`sim/beamline.py::SimBeamline`** — in-process `ophyd.sim` devices + the global identifiers
  the `smi_plans` plans expect. Drives *plan validation*.

Neither touches real hardware. The config (`config/microscope.yaml`, via `$BEAM_IMAGE_CONFIG`)
points the microscope at the fake IOC by default.

## The vendored microscope

`swaxs-beam-image`'s package is vendored under `microscope/` (its `app.py`/`__main__.py`
dropped). `microscope/builder.py` re-assembles it as an **embeddable component** —
`build_microscope()` returns the layout plus the live `InteractiveMode` bookmark store, so the
host app harvests bookmark `(x, y, z)` positions into `spec.samples.rows` (mapping onto
`{motor_object}_x/y/z`). Its own modes (click-to-move, bookmarks, square/polygon/line grids,
focus, calibrate) are unchanged and still emit their own microfocus/alignment snippets via the
color-coded script panel.

## Framework choice

Consolidated on **Panel/Bokeh**: the interactive sample builder needs a live camera figure
(Bokeh), and a single framework keeps the interview, refine, samples, and script tabs in one
app. The earlier Qt / NiceGUI / dashboard mockups and the A–O `techniques`/`guidance` core were
retired.

## The queueserver seam (designed for, not built)

`codegen.to_queueserver_item(spec)` returns `{"name": "acquire_from_spec", "kwargs": {"spec":
…}, "item_type": "plan"}`. A worker-side `acquire_from_spec(spec_dict)` plan would resolve names
→ devices. Because the spec is pure data and names-only, this is purely additive: a new consumer
of the same spec, with the GUI untouched. An `Executor` abstraction (`CopyPasteExecutor` now,
`QueueServerExecutor` later) is the intended insertion point.
