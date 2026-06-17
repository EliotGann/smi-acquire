"""Mode protocol and helper for the mode-selector → active-mode dispatch."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import panel as pn


@runtime_checkable
class Mode(Protocol):
    """Each interactive mode (Move/Bookmark / Square / Polygon / Linear / Focus / Calibrate) implements this.

    The app owns one figure, one beam overlay, one calibration, one stage. Modes are passive
    consumers — they receive tap events and update their own overlays / side panels.
    """

    name: str

    @property
    def panel(self) -> pn.viewable.Viewable: ...

    def activate(self) -> None: ...

    def deactivate(self) -> None: ...

    def on_tap(self, x: float, y: float) -> None: ...

    def tick(self) -> None:
        """Fast per-frame refresh (~10 Hz). Cheap operations only — image overlays / CDSes."""

    def tick_table(self) -> None:
        """Slower refresh (~1 Hz) for heavyweight widgets that don't need 10 Hz updates."""
