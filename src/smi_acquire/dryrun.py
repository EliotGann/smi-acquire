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

# Where to find the smi_plans package off-beamline (best-effort; the env may already have it).
_SMI_PLANS_DIRS = [
    os.environ.get("SMI_PLANS_PATH", ""),
    "/nsls2/users/egann/git/smi/smi-plans/src",
    "/nsls2/users/egann/git/smi/scripts/SWAXS_user_scripts/templates",
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
    script = codegen.render(spec, run=True)
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

    collected = {"msgs": []}

    def _RE(plan):
        msgs = list(plan)
        collected["msgs"] = msgs
        return msgs

    ns = dict(sim.globals_dict())
    ns["RE"] = _RE
    ns["__name__"] = "__smi_acquire_dryrun__"

    import warnings as _w
    try:
        with _w.catch_warnings(record=True) as caught:
            _w.simplefilter("always")
            exec(compile(script, "<generated>", "exec"), ns)  # noqa: S102 (sandboxed sim)
        msgs = collected["msgs"]
        o, c = sim.run_count(msgs)
        events = sim.primary_events(msgs)
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


def dry_run_experiment(project, experiment) -> DryRunReport:
    """Dry-run one :class:`~smi_acquire.project.Experiment` over its Project target subset."""
    return dry_run(project.experiment_spec(experiment))


__all__ = ["DryRunReport", "dry_run", "dry_run_experiment"]
