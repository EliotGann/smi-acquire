"""Affine pixel↔motor calibration (rotation-aware).

``CalibrationModel`` wraps a 2×2 matrix ``A`` such that
``pixel_delta = A @ motor_delta`` for a sample feature in the camera frame. The beam pixel
position acts as the translational offset for click-to-move math.

Rotation coupling
-----------------
The stage stacks rotate the in-plane motor axes as seen by the camera: the SMI ``chi`` axis
(rotation about the image-plane normal) spins the x/y motor frame.  Because the calibration is
fit at ``chi = 0`` (the reference), driving click-to-move at a non-zero ``chi`` requires rotating
the mapping by that angle.  ``click_to_motor_delta(..., chi_deg=θ)`` applies ``R(-θ)`` to the
motor delta so the clicked feature still lands under the beam when the frame is rotated.
"""

from __future__ import annotations

import numpy as np

from .config import CalibrationConfig


def _rot(theta_deg: float) -> np.ndarray:
    """2×2 active rotation matrix for ``theta_deg`` degrees."""
    t = np.deg2rad(float(theta_deg))
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, -s], [s, c]], dtype=float)


class CalibrationModel:
    def __init__(self, matrix: np.ndarray | list[list[float]]) -> None:
        A = np.asarray(matrix, dtype=float)
        if A.shape != (2, 2):
            raise ValueError(f"calibration matrix must be 2x2, got {A.shape}")
        self._A = A
        self._A_inv = np.linalg.inv(A)

    @classmethod
    def from_config(cls, cfg: CalibrationConfig) -> "CalibrationModel":
        return cls(cfg.matrix)

    @property
    def matrix(self) -> np.ndarray:
        return self._A.copy()

    def motor_to_pixel_delta(self, dm: np.ndarray | tuple[float, float]) -> np.ndarray:
        return self._A @ np.asarray(dm, dtype=float)

    def pixel_to_motor_delta(self, dp: np.ndarray | tuple[float, float]) -> np.ndarray:
        return self._A_inv @ np.asarray(dp, dtype=float)

    def click_to_motor_delta(
        self,
        click_px: tuple[float, float],
        beam_px: tuple[float, float],
        chi_deg: float = 0.0,
    ) -> np.ndarray:
        """Motor delta required to move the clicked feature to under the beam.

        The feature at ``click_px`` must shift in the image by ``(beam_px - click_px)`` pixels.
        At ``chi = 0`` that shift is produced by a motor delta of ``A_inv @ (beam_px - click_px)``.

        When the in-plane frame is rotated by ``chi_deg`` (the calibration was fit at chi = 0),
        the motor axes are rotated with it, so the required motor delta is rotated back by
        ``-chi_deg``: ``R(-chi) @ A_inv @ (beam_px - click_px)``.  Pass the appropriate chi sum
        (Huber chi for the Huber stack; Huber chi + piezo chi for the piezo stack).
        """
        dp = np.array(
            [beam_px[0] - click_px[0], beam_px[1] - click_px[1]],
            dtype=float,
        )
        dm = self._A_inv @ dp
        if chi_deg:
            dm = _rot(-chi_deg) @ dm
        return dm

    def update_matrix(self, matrix: np.ndarray | list[list[float]]) -> None:
        A = np.asarray(matrix, dtype=float)
        if A.shape != (2, 2):
            raise ValueError(f"calibration matrix must be 2x2, got {A.shape}")
        self._A = A
        self._A_inv = np.linalg.inv(A)
