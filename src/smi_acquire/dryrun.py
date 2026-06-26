"""
smi_acquire.dryrun
==================

Exercise a generated script against the simulated beamline — **no hardware, no RunEngine** —
and report what it would do, so the GUI can show "✅ 1 run/sample, N events" or surface the
exact error before the user takes beam.

Mirrors the validation approach in ``smi-plans/skills/smi-plans-gui-builder.md``: build the
:class:`~smi_acquire.sim.beamline.SimBeamline`, inject its globals into the ``smi_plans``
modules (so ``saxs_waxs_dets()`` / the heater factories resolve), then ``exec`` the generated
script with ``RE`` swapped for a collector that simply exhausts the plan and counts messages.
Running the *generated text* (not the spec directly) validates the codegen too.
"""

from __future__ import annotations

import os
import sys
import importlib
from dataclasses import dataclass, field
from typing import List, Optional

from .spec import ExperimentSpec
from . import codegen

# Where to find the smi_plans package off-beamline (best-effort; the env normally has it as an
# editable install). Fallback points at OUR checkout (egann's is not group-readable).
_SMI_PLANS_DIRS = [
    os.environ.get("SMI_PLANS_PATH", ""),
    "/home/xf12id/git/smi/smi-plans/src",
]


@dataclass
class DryRunReport:
    ok: bool
    runs: int = 0
    events: int = 0
    n_samples: int = 1
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None
    script: str = ""

    def summary(self) -> str:
        if not self.ok:
            return "❌ {}".format(self.error)
        head = "✅ {} run{} / {} event{}".format(
            self.runs, "" if self.runs == 1 else "s",
            self.events, "" if self.events == 1 else "s")
        if self.warnings:
            head += "   ⚠️ {} warning{}".format(
                len(self.warnings), "" if len(self.warnings) == 1 else "s")
        return head


def _ensure_smi_plans() -> bool:
    try:
        import smi_plans  # noqa: F401
        return True
    except Exception:
        pass
    for d in _SMI_PLANS_DIRS:
        if d and os.path.isdir(os.path.join(d, "smi_plans")):
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                import smi_plans  # noqa: F401
                return True
            except Exception:
                continue
    return False


def _inject(sim) -> None:
    """Inject sim globals into every loaded smi_plans.* module (device-dependent cores first)."""
    g = sim.globals_dict()
    for base in ("smi_plans._core", "smi_plans._compose", "smi_plans._preprocessors",
                 "smi_plans.technique_C_temperature"):
        try:
            mod = importlib.import_module(base)
            for k, v in g.items():
                setattr(mod, k, v)
        except Exception:
            pass
    for name, mod in list(sys.modules.items()):
        if name.startswith("smi_plans") and mod is not None:
            for k, v in g.items():
                try:
                    setattr(mod, k, v)
                except Exception:
                    pass


def dry_run(spec: ExperimentSpec) -> DryRunReport:
    """Render ``spec`` → script, exec it under the sim, and report runs/events/warnings/errors."""
    # Inline named-list values (for_dryrun) so the script execs under the sim with no Redis.
    script = codegen.render(spec, run=True, for_dryrun=True)
    n_samples = spec.n_samples()
    spec_warnings = spec.order_warnings()

    if not _ensure_smi_plans():
        return DryRunReport(
            ok=False, n_samples=n_samples, warnings=spec_warnings, script=script,
            error="smi_plans not importable (set SMI_PLANS_PATH); static checks only")

    try:
        from .sim.beamline import SimBeamline
        sim = SimBeamline()
    except Exception as exc:
        return DryRunReport(
            ok=False, n_samples=n_samples, warnings=spec_warnings, script=script,
            error="simulated beamline unavailable: {}".format(exc))

    _inject(sim)

    # Drive the generated plan through a REAL RunEngine and assert on emitted DOCUMENTS.
    #
    # The smi_plans plans are message-pure (they use ``bps.rd`` / ``bps.mv``), so a bare
    # ``list(plan)`` CANNOT answer ``rd``/``read`` messages -- it returns the default (0), which
    # makes read-and-branch loops (the temperature/flux equilibration in ``_compose``) spin
    # forever.  This mirrors ``smi-plans/tests/conftest.py``: a RunEngine feeds readbacks back
    # into the generator; ``bps.sleep`` is made instant so settle/soak waits don't block; manual
    # ``input`` prompts are answered non-interactively; runs/events are counted from documents.
    from collections import Counter
    import builtins
    import bluesky.plan_stubs as _bps
    from unittest import mock
    from bluesky import RunEngine

    docs: List[tuple] = []

    def _RE(plan):
        RE = RunEngine({})
        RE.subscribe(lambda name, doc: docs.append((name, doc)))

        def _instant_sleep(t):
            yield from _bps.null()

        with mock.patch.object(_bps, "sleep", _instant_sleep), \
                mock.patch.object(builtins, "input", lambda prompt="": ""):
            RE(plan)
        return docs

    def _count(document_list):
        names = [n for n, _ in document_list]
        opens, closes = names.count("start"), names.count("stop")
        stream = {d["uid"]: d.get("name", "primary")
                  for n, d in document_list if n == "descriptor"}
        ev = Counter()
        for n, d in document_list:
            if n == "event":
                ev[stream.get(d["descriptor"], "primary")] += 1
        return opens, closes, ev.get("primary", 0)

    ns = dict(sim.globals_dict())
    ns["RE"] = _RE
    ns["__name__"] = "__smi_acquire_dryrun__"

    import warnings as _w
    try:
        with _w.catch_warnings(record=True) as caught:
            _w.simplefilter("always")
            exec(compile(script, "<generated>", "exec"), ns)  # noqa: S102 (sandboxed sim)
        o, c, events = _count(docs)
        warns = list(spec_warnings)
        for w in caught:
            warns.append(str(w.message))
        ok = (o == c) and o >= 1
        err = None if ok else "unbalanced run: {} open / {} close".format(o, c)
        return DryRunReport(ok=ok, runs=o, events=events, n_samples=n_samples,
                            warnings=warns, error=err, script=script)
    except Exception as exc:
        return DryRunReport(
            ok=False, n_samples=n_samples, warnings=spec_warnings, script=script,
            error="{}: {}".format(type(exc).__name__, exc))


__all__ = ["DryRunReport", "dry_run"]


def dry_run_experiment(project, experiment, store) -> DryRunReport:
    """Dry-run one :class:`~smi_acquire.project.Experiment` over its target subset.

    ``store`` is an :class:`smi_acquire.store.AcquireStore`; the target is resolved against the
    shared sample store.
    """
    return dry_run(project.experiment_spec(experiment, store))


__all__ = ["DryRunReport", "dry_run", "dry_run_experiment"]
