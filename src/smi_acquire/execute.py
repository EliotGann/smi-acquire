"""
smi_acquire.execute
===================

The **execution seam** — the single boundary through which the app causes motion or submits a
plan, designed so the eventual switch to the **bluesky-queueserver** is additive (a new backend
class), not a rewrite.

Today's reality (no qserver yet)
--------------------------------
Two distinct paths, per the agreed model:

* **Jogging / click-to-move** runs *here*, directly, via ``ophyd.EpicsMotor.set()`` —
  lightweight and working.  This is the **only** motion this app performs, and it is **gated by
  the** :class:`~smi_acquire.interlock.Interlock` so it locks out while an external RunEngine is
  running.  (Conflicting messages are still possible; the interlock mitigates it, and the whole
  path disappears when qserver lands.)
* **Everything else** (quick alignment scans, composed experiments) is **not executed here**.
  The app produces a **copy-paste ``RE(...)`` script** for the dedicated RunEngine running in the
  beamline session.  No second RunEngine lives in this app.

The seam
--------
``Executor`` has two kinds of method:

* ``jog(motor, delta)`` / ``move_abs(motor, target)`` / ``stop(motor)`` — *immediate* motion
  (only the local backend implements these; the others refuse).
* ``submit(payload)`` — hand off a *plan* (a generated script, or later a spec/queue item).
  Returns a :class:`Submission` describing what the user should do (copy this text / it was
  enqueued).

Backends:

* :class:`LocalExecutor`     — NOW. Jogs directly (interlock-gated); ``submit`` returns the
  script text to copy into the beamline session.
* :class:`QueueServerExecutor` — LATER (stub). ``submit`` would enqueue a plan item via
  ``bluesky-queueserver``'s ``REManagerAPI``; jogging would route to the queue too. Raises
  ``NotImplementedError`` with the intended shape documented, so the GUI can already offer a
  (disabled) "Submit to queue" button.

Nothing here imports bluesky.  ``ophyd`` is only touched inside ``LocalExecutor`` jog methods
(and only when actually jogging), so the module imports cleanly everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------
@dataclass
class Submission:
    """The outcome of an ``Executor.submit`` — what to show/do, not a running plan."""
    kind: str                       # "copy" (paste this text) | "queued" | "error"
    text: str = ""                  # script to copy, or a queue-item id, or an error message
    detail: str = ""                # human note for the UI

    @property
    def ok(self) -> bool:
        return self.kind != "error"


class InterlockedError(RuntimeError):
    """Raised when a direct action is attempted while the external RunEngine is busy."""


# ---------------------------------------------------------------------------
# the interface
# ---------------------------------------------------------------------------
class Executor:
    """Abstract execution backend (motion + plan submission).

    The GUI holds exactly one ``Executor`` and calls it for every motion/submit; swapping the
    backend (local → queueserver) is the qserver "switch".
    """

    name = "executor"
    can_jog = False                 # whether immediate jog/move is supported by this backend

    # -- immediate motion (interactive alignment) ---------------------------
    def jog(self, motor, delta: float):
        raise NotImplementedError

    def move_abs(self, motor, target: float):
        raise NotImplementedError

    def stop(self, motor):
        raise NotImplementedError

    # -- plan submission ----------------------------------------------------
    def submit(self, payload: Any) -> Submission:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# NOW: local — direct ophyd jog (interlock-gated) + copy-paste submit
# ---------------------------------------------------------------------------
class LocalExecutor(Executor):
    """The interim backend: direct ``ophyd`` jogging + copy-paste-to-beamline submission.

    ``interlock`` is an :class:`~smi_acquire.interlock.Interlock`; when it reports busy, the
    immediate-motion methods refuse (raise :class:`InterlockedError`) so the app cannot fight an
    external RunEngine.  ``submit`` never executes — it returns the script text to copy.
    """

    name = "local (direct ophyd + copy-paste)"
    can_jog = True

    def __init__(self, interlock=None):
        self.interlock = interlock

    def _check(self, what: str):
        il = self.interlock
        if il is not None and il.is_busy():
            raise InterlockedError(
                "{} is locked out: an external RunEngine is running. {}".format(
                    what, il.banner()))

    # -- immediate motion ---------------------------------------------------
    def jog(self, motor, delta: float):
        """Relative move ``motor`` by ``delta`` (returns the ophyd Status)."""
        self._check("jog")
        return motor.set(motor.position + float(delta))

    def move_abs(self, motor, target: float):
        """Absolute move ``motor`` to ``target`` (returns the ophyd Status)."""
        self._check("move")
        return motor.set(float(target))

    def stop(self, motor):
        # stop is always allowed (it's a safety action), even under interlock.
        return motor.stop()

    # -- submission (copy-paste; no execution here) -------------------------
    def submit(self, payload: Any) -> Submission:
        """``payload`` is the generated script text; return it for the user to copy/paste."""
        text = payload if isinstance(payload, str) else str(payload)
        return Submission(
            kind="copy", text=text,
            detail="Copy into the beamline IPython session and run there (no RunEngine here).")


# ---------------------------------------------------------------------------
# LATER: queueserver (stub) — the qserver "switch"
# ---------------------------------------------------------------------------
class QueueServerExecutor(Executor):
    """Planned backend for ``bluesky-queueserver`` (NOT built — qserver is not in use at SMI).

    When a queueserver exists, this backend will:

    * ``submit(spec_or_item)`` → ``RM.item_add({"name": "acquire_from_spec",
      "kwargs": {"spec": ...}, "item_type": "plan"})`` against a ``REManagerAPI`` (the spec is
      already pure-data/names-only for exactly this — see ``smi_acquire.codegen``), returning the
      queue item uid.
    * ``jog/move_abs`` → enqueue a small ``mv`` plan (or use the manager's direct-control API),
      so even interactive moves go through the one RunEngine the worker owns — at which point the
      local interlock is unnecessary (there is only one mover).

    Constructing or calling it now raises ``NotImplementedError`` so the GUI can present a
    disabled "Submit to queue" affordance without a backend.
    """

    name = "queueserver (not yet available)"
    can_jog = False

    def __init__(self, manager: Optional[Any] = None):
        self.manager = manager

    def _nope(self):
        raise NotImplementedError(
            "the queueserver backend is not built yet (qserver is not in use at SMI). "
            "This is the documented future seam; use the copy-paste path for now.")

    def jog(self, motor, delta: float):
        self._nope()

    def move_abs(self, motor, target: float):
        self._nope()

    def stop(self, motor):
        self._nope()

    def submit(self, payload: Any) -> Submission:
        self._nope()


__all__ = ["Executor", "LocalExecutor", "QueueServerExecutor", "Submission", "InterlockedError"]
