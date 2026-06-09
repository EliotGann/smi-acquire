"""
smi-acquire -- a flexible sample-list + technique-picker + script-generator for the NSLS-II
SMI-SWAXS endstation.

The package is a **headless core** (pure Python, no GUI, no bluesky) that every front-end
(Panel / Qt / NiceGUI) consumes:

* :mod:`smi_acquire.samples`    -- the GUI-facing Sample / SampleList model (sourced from
  ``smi_plans`` when available) + table <-> object helpers.
* :mod:`smi_acquire.techniques` -- declarative A--O technique registry (params + entry points).
* :mod:`smi_acquire.guidance`   -- "which technique do I want?" recommendation rules.
* :mod:`smi_acquire.codegen`    -- (SampleList, technique, params) -> runnable script string.
"""

from . import samples, techniques, guidance, codegen  # noqa: F401

__all__ = ["samples", "techniques", "guidance", "codegen"]
__version__ = "0.0.1"
