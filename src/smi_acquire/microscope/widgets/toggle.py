"""Reusable latching on/off toggle with an unmistakable pressed-state cue.

A plain ``button_type="success"`` :class:`~panel.widgets.Toggle` barely changes appearance
between its pressed/unpressed states, so a latching on/off control is hard to read. These
helpers restyle the widget per state:

- **ON**  — solid green button, check icon, ``<label> · ON``.
- **OFF** — hollow/outline grey button, empty-circle icon, ``<label> · OFF``.

The base label is stashed on the widget (``toggle._latch_label``) so :func:`style_latching_toggle`
can re-derive the text on every flip. Build with :func:`make_latching_toggle` and call
:func:`style_latching_toggle` from your ``value`` watcher.
"""

from __future__ import annotations

import panel as pn


def style_latching_toggle(toggle: pn.widgets.Toggle) -> None:
    """Apply the solid-green-ON / outline-grey-OFF look based on ``toggle.value``."""
    label = getattr(toggle, "_latch_label", "")
    if toggle.value:
        toggle.button_type = "success"
        toggle.button_style = "solid"
        toggle.icon = "check"
        toggle.name = f"{label} \u00b7 ON"
    else:
        toggle.button_type = "default"
        toggle.button_style = "outline"
        toggle.icon = "circle"
        toggle.name = f"{label} \u00b7 OFF"


def make_latching_toggle(
    label: str, value: bool = False, width: int = 200
) -> pn.widgets.Toggle:
    """Build a latching toggle showing ``label`` with the ON/OFF styling pre-applied.

    Callers should also call :func:`style_latching_toggle` from their own ``value`` watcher
    so the look updates whenever the toggle is flipped.
    """
    toggle = pn.widgets.Toggle(value=value, width=width)
    toggle._latch_label = label
    style_latching_toggle(toggle)
    return toggle
