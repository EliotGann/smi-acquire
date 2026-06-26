# smi-plans follow-ups owed by the Redis-by-name refactor

This is the precise, actionable list of changes the **smi-acquire** GUI refactor (Redis-by-name +
priority + per-sample project + named lists) needs from the sibling **smi-plans** repo
(`/home/xf12id/git/smi/smi-plans`, package `smi_plans`). The GUI works **today** without them via
stopgaps noted below; these close the loop so the runtime matches the GUI and the by-name calls
read cleanly.

Owner decision (this round): *smi-acquire now, smi-plans separately.* Nothing here is implemented
in smi-acquire beyond the stopgaps.

---

## 1. `Sample.priority` native field + `load_holder` primary sort  ★ highest value

**Why:** the GUI shows the master sample list in **run order** (a per-sample `priority`, lower
first) and `project.resolve_target` emits the bar in that order. But the *runtime* order of
`load_holder("bar")` is decided **only** in `smi_plans/_holder.py` `load_holder._key`
(`_holder.py:91-102`), which sorts by `(holder.sample_ids order, slot, name)` — it does **not**
know about priority. So a generated `load_holder('bar')` script runs in holder/slot order, not the
GUI's priority order.

**Stopgap in smi-acquire:** priority lives on `Sample.md['priority']`; codegen sorts the rows it
emits, so order is honored only when the bar is materialized from those rows (it is for
`from_columns`; for `load_holder` it is **not** until this lands).

**Change:**
- Add `priority: int = 0` to `Sample` (`smi_plans/_samples.py:482-519`); emit in `to_dict`
  (`:609-632`), read in `from_dict` (`:634-668`, `d.get("priority", 0)` — back-compatible),
  coerce in `__post_init__` (`:521-545`).
- Make it the **primary** key in `load_holder._key` (`_holder.py:93-102`):
  `return (priority, primary, slot_rank, name)` (lower priority first).
- Optionally add `SampleList.sort_by_priority()` (`_samples.py` near `:671`).
- Tests: update `tests/test_holder.py:18-19,32-37` (the order contract) + a priority-order case.

**Then in smi-acquire:** migrate `Sample.md['priority']` → the native field (a tiny change in
`apps/acquire_app.py::_sample_priority`/`_set_sample_priority` and `project.py::_sample_priority`).

---

## 2. `project_name` carried on **every** scan (and may vary per sample)

**Why:** the requirement is that every generated run carries a `project_name`, and project may
differ per sample. Today `acquire`/`acquire_bar` only pass through whatever `md` they're given
(`_compose.acquire` merges `md`; `acquire_bar` does `md=merge_md(md, s.md)` at `_compose.py:521`),
so a missing project is silently absent.

**Stopgap in smi-acquire:** per-sample project lives on `Sample.md['project_name']` and rides into
each run via `acquire_bar`'s `merge_md(md, s.md)` (verified); codegen also emits a run-level
`md={'project_name': …}` fallback. This already produces correct per-sample project today.

**Change (smi-plans):** decide whether to *enforce* project_name presence (e.g. `acquire(...,
require_project=True)` or a clear warning when neither run-md nor sample-md carries one), so a
script that forgets it fails loudly rather than filing runs with no project. This is a policy call
for the backend; the GUI already always supplies one.

---

## 3. `resolve_list` default store (so the by-name call reads cleanly)

**Why:** `resolve_list(name, kind=…)` **raises** when given a name with no `store=`
(`_lists.py:283-287`) — unlike `load_holder`, which auto-opens `SampleStore.from_redis()` when
`store is None` (`_holder.py:78-79`). So the elegant `resolve_list("Fe_K_XANES", kind="energy")`
from the GUI skill doesn't actually run.

**Stopgap in smi-acquire:** codegen emits an explicit `lists = ListStore.from_redis()` and passes
`store=lists` to every `resolve_list(...)` (see `codegen.py::_list_values_src` + the `lists = …`
line). Works, but is more verbose than the skill's example.

**Change (smi-plans):** give `resolve_list` a default-store path mirroring `load_holder` — when
`store is None` and `value` is a name, open `ListStore.from_redis()` (lazy, `[beamline]` extra).
Then the generated call can drop `store=lists` and the import.

---

## 4. Per-kind spec→values builders for `temperature` and richer `incidence`

**Why:** the GUI's per-type editors store rich, editable recipes in `NamedList.spec` /
`NamedList.md` — temperature carries `{values|range, cycle}` + md `{ramp_rate, soak, first_soak}`;
incidence carries `{range}` or `{values}`. The backend can only **re-materialize** `spec→values`
for `energy` (edge) and the generic linspace (`_lists.py:_SPEC_BUILDERS`, `:92-97`). For
temperature's `cycle` (anneal-then-cool doubling) and any future incidence shaping, there is no
builder.

**Current safety:** the GUI always writes authoritative `values` (post-cycle), so `resolve_list`
returns the right list **without** a builder. The gap only matters if something other than the GUI
wants to rebuild these lists from `spec` alone.

**Change (smi-plans):** add builders to `_SPEC_BUILDERS` for `temperature` (honoring
`{values|range, cycle}` — cycle = `pts + pts[::-1]`) and any incidence shaping, matching the GUI's
spec shape (energy already matches `energy_grid`). Keep them pure-Python/CI-testable.

> Spec-shape note: the GUI's energy editor uses `{boundaries, steps}` (+ `updown`), while the
> backend's energy builder uses `{edge, pre, near, post}`. They are **different parameterizations**;
> since `values` is authoritative this is fine, but if backend re-materialization of the GUI's
> energy lists is ever wanted, reconcile the two spec shapes.

---

## 5. A shared **proposal** redis key the GUI can read (read-only)

**Why:** the GUI shows the proposal/data-session **read-only** (it must not import the
profile/RE.md — it's a separate process). `smi_acquire/proposal.py` reads a configurable redis key
(`SMI_ACQUIRE_PROPOSAL_KEY` / `SMI_ACQUIRE_PROPOSAL_DB`) and degrades to "—" when unset.

**Change (profile/facility, tracked here):** publish the current proposal/`data_session` (the one
`proposal_id(...)` sets in `RE.md`) into a shared redis key both the session and the GUI can read —
consistent with the deferred facility shared-proposal mechanism in
`smi-plans/docs/QSERVER_WIRING.md` → "Deferred: proposal/project metadata". Once the key exists,
point `SMI_ACQUIRE_PROPOSAL_KEY` at it. No smi-plans code change is strictly required — this is a
deployment/profile detail — but it belongs with the proposal/metadata work.

---

## 6. (Noted, not requested) a `time` named-list kind

`NamedList` lists `time` as a kind (`_lists.py:36`), intended for exposure/period lists. But
smi-acquire's `time` axis is `time_axis(n_frames, *, period)` (`_compose.py:857`) — a **count +
period**, not a value list — so `resolve_list` doesn't apply, and the GUI's time editor was left as
the plain n_frames/period form (no name/save/open). If an exposure-time **list** axis is ever
wanted, it needs a new axis builder in smi-plans first; only then does a `time` named-list editor
make sense.
