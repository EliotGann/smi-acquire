"""
smi-acquire — an **interrogation-driven acquisition builder** for the NSLS-II SMI-SWAXS
endstation.

Rather than choosing one of a fixed A–O menu, the user is *interrogated* — a guided interview
asks what they need (beam/q, geometry/environment, what they're varying, how samples are
handled) and **assembles a bespoke plan** as a serializable :class:`ExperimentSpec`. Sample
positions are built interactively with the vendored on-axis microscope (live camera,
click-to-move, bookmarks, grid/line/alignment scans) against a **fake IOC** — no real hardware.

Headless core (pure Python; no Panel):

* :mod:`smi_acquire.store`     — the boundary onto the shared **redis db=2 sample store**
  (``smi_plans.SampleStore``): holders + samples + the active-sample pointer live there.
* :mod:`smi_acquire.lists`     — the boundary onto the shared **redis db=2 named-list store**
  (``smi_plans.ListStore``): reusable energy/incidence/temperature/time lists referenced by name.
* :mod:`smi_acquire.project`   — the **local** session document: transient experiment *recipes*
  (which target a holder / all samples) + local reference fiducials.
* :mod:`smi_acquire.spec`      — the ``ExperimentSpec`` scan-recipe model codegen/dryrun consume.
* :mod:`smi_acquire.registry`  — the SMI device / detector / axis-concern catalog.
* :mod:`smi_acquire.interview` — the interrogation: questions → a tailored starting recipe.
* :mod:`smi_acquire.codegen`   — ``ExperimentSpec`` → runnable ``smi_plans`` script.
* :mod:`smi_acquire.dryrun`    — exec the script against a simulated beamline; report runs/events.
* :mod:`smi_acquire.samples`   — Sample / SampleList bridge (sourced from ``smi_plans``).

Interactive + simulation:

* :mod:`smi_acquire.microscope` — vendored on-axis microscope (Panel/Bokeh) sample builder.
* :mod:`smi_acquire.sim`        — the fake caproto IOC + in-process ``SimBeamline``.
"""

from . import spec, registry, interview, codegen, samples, project, store, lists  # noqa: F401

__all__ = ["spec", "registry", "interview", "codegen", "samples", "project", "store", "lists"]
__version__ = "0.1.0"
