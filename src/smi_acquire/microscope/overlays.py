"""Bokeh glyphs that ride on top of the camera image.

Each overlay owns one ColumnDataSource and is attached to the figure via a renderer method
(``add_to``). Updates mutate the CDS in place so the renderer keeps working without rebuilds.
"""

from __future__ import annotations

from bokeh.models import ColumnDataSource
from bokeh.plotting import figure as Figure  # noqa: N812 — used as a type alias only


class BeamOverlay:
    """A single rectangle representing the beam footprint at a fixed pixel center."""

    def __init__(self, center_px: tuple[float, float], width_px: float, height_px: float) -> None:
        cx, cy = center_px
        self.cds = ColumnDataSource(
            data={"x": [cx], "y": [cy], "w": [width_px], "h": [height_px]}
        )

    def add_to(self, fig: Figure) -> None:
        fig.rect(
            x="x",
            y="y",
            width="w",
            height="h",
            source=self.cds,
            fill_color="yellow",
            fill_alpha=0.25,
            line_color="yellow",
            line_width=2,
        )

    def update(
        self,
        center_px: tuple[float, float] | None = None,
        width_px: float | None = None,
        height_px: float | None = None,
    ) -> None:
        data = dict(self.cds.data)
        if center_px is not None:
            data["x"] = [center_px[0]]
            data["y"] = [center_px[1]]
        if width_px is not None:
            data["w"] = [width_px]
        if height_px is not None:
            data["h"] = [height_px]
        self.cds.data = data

    @property
    def center(self) -> tuple[float, float]:
        return float(self.cds.data["x"][0]), float(self.cds.data["y"][0])
