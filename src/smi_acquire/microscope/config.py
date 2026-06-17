"""Configuration schema and YAML I/O.

The app loads a YAML file (env var ``BEAM_IMAGE_CONFIG`` or ``config/default.yaml``) into a
pydantic ``AppConfig`` at startup. Edits made through the UI write back to the same file.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

# Vendored into smi-acquire: this file lives at
# ``src/smi_acquire/microscope/config.py`` so the repo-root ``config/`` dir is parents[3].
_REPO_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
DEFAULT_CONFIG_PATH = _REPO_CONFIG_DIR / "microscope.yaml"
LOCAL_CONFIG_PATH = _REPO_CONFIG_DIR / "microscope.local.yaml"


class BeamPreset(BaseModel):
    width_px: float
    height_px: float


class BeamConfig(BaseModel):
    center_px: tuple[float, float] = (320.0, 240.0)
    width_px: float = 40.0
    height_px: float = 8.0
    presets: dict[str, BeamPreset] = Field(default_factory=dict)


class EpicsConfig(BaseModel):
    camera_prefix: str
    cam_suffix: str = "cam1:"
    # Image source. Two protocols supported:
    #   "ca"  → image_pv is appended to camera_prefix to form the CA ArrayData PV; e.g.
    #           with image_pv="image1:ArrayData" the full PV is `<prefix>image1:ArrayData`.
    #           ArraySize0/1/2 of the image plugin are read via CA from `<prefix>image1:`.
    #   "pva" → image_pv is appended to camera_prefix to form a PVAccess NTNDArray PV; e.g.
    #           "Pva1:Image" gives `<prefix>Pva1:Image`. Dimensions + color mode come from
    #           the structured value itself, no separate PVs needed.
    image_protocol: str = "ca"  # "ca" | "pva"
    image_pv: str = "image1:ArrayData"
    # The PRIMARY in-plane alignment axes the camera click-to-move drives (x/y/z). By the SMI
    # stack these are normally the piezo fine axes; kept as ``motors`` for back-compat (every
    # microscope mode uses stage.x/.y/.z).
    motors: dict[str, str]
    # The full stacked stage, so a captured position records EVERY axis (SAMPLE_SYSTEM_PLAN
    # Position has piezo_* + stage_*). Optional: absent axes are simply not captured/moved.
    #   piezo_motors: the SmarAct fine stage  {x,y,z,th,chi}
    #   stage_motors: the Huber coarse stage  {x,y,z,theta,chi,phi}
    # When given, ``motors`` (the click-to-move axes) usually duplicates piezo_motors' x/y/z.
    piezo_motors: dict[str, str] = Field(default_factory=dict)
    stage_motors: dict[str, str] = Field(default_factory=dict)
    sample_name_pv: str | None = None


class CalibrationConfig(BaseModel):
    # pixel = matrix @ motor + offset_implicit_from_beam_center
    matrix: list[list[float]] = Field(default_factory=lambda: [[1.0, 0.0], [0.0, 1.0]])


class UIConfig(BaseModel):
    poll_hz: float = 10.0
    # All motor-axis values (step, target, Δ) are in *motor units* — whatever your EpicsMotor
    # uses natively. Set ``motor_units`` so the labels match. Common values: "mm", "µm",
    # "um", "deg". This is purely cosmetic — calibration math is unit-agnostic since the
    # affine matrix absorbs the px/(motor unit) ratio.
    motor_units: str = "mm"
    default_step: float = 0.05  # numeric default, in motor_units
    max_grid_points: int = 100_000
    image_size_hint: tuple[int, int] = (640, 480)
    # Server-side subsample so the WebSocket payload stays sane. Large industrial cameras
    # (3296×2472 = 24 MB/frame RGB) will saturate the browser's WS connection at native res.
    # We pick an integer stride so the displayed image is ≤ display_max_dim in either axis.
    display_max_dim: int = 1280

    @model_validator(mode="before")
    @classmethod
    def _migrate_default_step_mm(cls, data):
        # Back-compat: accept the old `default_step_mm` field name if present.
        if isinstance(data, dict) and "default_step_mm" in data and "default_step" not in data:
            data["default_step"] = data.pop("default_step_mm")
        return data


class ScriptsConfig(BaseModel):
    """Identifiers baked into the emitted bluesky-plan snippets.

    The snippets are complete ``def`` plans the user can paste into a beamline script. We
    define ``dets`` inline as a Python list so each snippet is self-contained other than
    requiring ``bp``, ``bps``, ``piezo``, ``smi``, and the named detector objects in scope.
    """

    # Object whose attributes carry the motor axes; emitted as ``{motor_object}.x``, ``.y``,
    # ``.z``. The same string is used as the prefix for motor-name placeholders in the
    # ``sample_name`` md template — e.g. with motor_object="piezo" you get ``{piezo_x}``.
    motor_object: str = "stage"
    # Python identifiers placed inside the inline ``dets = [...]`` list for plan-mode scans
    # (bookmarks, polygon, square). Filename-signal is appended by code where needed.
    detectors: list[str] = Field(default_factory=lambda: ["det"])
    # Detectors used by alignment / knife-edge scans (direct-beam imaging).
    alignment_detectors: list[str] = Field(default_factory=lambda: ["det"])
    # Wrapped before/after the alignment scan via ``yield from ...``. Leave empty strings
    # to skip — at SMI these are ``smi.modeAlignment()`` and ``smi.modeMeasurement()``.
    alignment_pre: str = ""
    alignment_post: str = ""
    # ``ophyd.Signal`` ``name=`` kwarg for the per-event filename signal driven during
    # bookmark scans. The same string is the placeholder inside the md template.
    filename_signal_name: str = "filename_template"

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data):
        if not isinstance(data, dict):
            return data
        # Old single-string fields → list defaults.
        if "list_detectors" in data and "detectors" not in data:
            # The old field was the *variable name* (always "dets"), not the list contents.
            # Fall back to a sensible default detector list.
            data.pop("list_detectors", None)
        if "alignment_detector" in data and "alignment_detectors" not in data:
            data["alignment_detectors"] = [str(data.pop("alignment_detector"))]
        if "sample_signal_name" in data and "filename_signal_name" not in data:
            data["filename_signal_name"] = str(data.pop("sample_signal_name"))
        return data


class ReferenceBookmark(BaseModel):
    """A persisted bookmark — a named (x, y, z) position that survives sessions.

    References behave like regular bookmarks visually (they're projected to pixels each
    tick and appear on the image), but they're **excluded** from generated bluesky scripts.
    Use them for stable landmarks: cross-hair targets, alignment fiducials, fixed sample
    holders, etc.
    """

    name: str
    x: float
    y: float
    z: float


class AppConfig(BaseModel):
    epics: EpicsConfig
    beam: BeamConfig = Field(default_factory=BeamConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    scripts: ScriptsConfig = Field(default_factory=ScriptsConfig)
    references: list[ReferenceBookmark] = Field(default_factory=list)

    _source_path: Path | None = None

    def save(self, path: Path | None = None) -> None:
        target = path or self._source_path or DEFAULT_CONFIG_PATH
        data = self.model_dump(mode="json")
        with open(target, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False)


def _resolve_config_path(path: str | os.PathLike | None) -> Path:
    """Pick the config file to load.

    Precedence:
        1. Explicit ``path`` argument.
        2. ``$BEAM_IMAGE_CONFIG`` environment variable.
        3. ``config/local.yaml`` (gitignored override) if it exists.
        4. ``config/default.yaml`` (the bundled defaults pointing at the fake IOC).
    """
    if path is not None:
        return Path(path)
    env = os.environ.get("BEAM_IMAGE_CONFIG")
    if env:
        return Path(env)
    if LOCAL_CONFIG_PATH.exists():
        return LOCAL_CONFIG_PATH
    return DEFAULT_CONFIG_PATH


def load_config(path: str | os.PathLike | None = None) -> AppConfig:
    resolved = _resolve_config_path(path)
    with open(resolved) as f:
        raw = yaml.safe_load(f)
    cfg = AppConfig.model_validate(raw)
    object.__setattr__(cfg, "_source_path", resolved)
    return cfg
