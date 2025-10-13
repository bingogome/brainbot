from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np


def _sorted_slices(section: Mapping[str, Any] | None) -> list[tuple[str, slice]]:
    if not section:
        return []
    entries: list[tuple[str, slice]] = []
    for name, spec in section.items():
        start = int(spec.get("start", 0))
        end = int(spec.get("end", start))
        entries.append((str(name), slice(start, end)))
    entries.sort(key=lambda item: item[1].start)
    return entries


class Gr00TObservationMapper:
    """Utility to reshape Brainbot observations into GR00T modality inputs."""

    def __init__(
        self,
        modality_config_path: Path,
        state_keys: Sequence[str],
        camera_keys: Sequence[str] | None = None,
    ):
        with modality_config_path.expanduser().open("r", encoding="utf-8") as handle:
            modality_config = json.load(handle)

        self._state_slices = _sorted_slices(modality_config.get("state"))
        if not self._state_slices:
            raise ValueError("Modality configuration is missing 'state' definitions")

        self._state_keys = list(state_keys)
        expected = max((sl.stop for _, sl in self._state_slices), default=0)
        if len(self._state_keys) < expected:
            raise ValueError(
                f"state_keys length ({len(self._state_keys)}) is smaller than "
                f"the modality expectation ({expected})"
            )

        video_section = modality_config.get("video") or {}
        if camera_keys:
            self._camera_keys = list(camera_keys)
        else:
            self._camera_keys = list(video_section.keys())
        if not self._camera_keys:
            raise ValueError("No camera keys supplied and modality config lacks 'video' section")

    def build(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        robot_raw = payload.get("robot")
        robot_data = dict(robot_raw) if isinstance(robot_raw, Mapping) else {}
        camera_group = {}
        cameras_field = robot_data.get("cameras")
        if isinstance(cameras_field, Mapping):
            camera_group = dict(cameras_field)
            # keep robot_data intact for downstream use

        result: dict[str, Any] = {}

        camera_arrays = {}
        for key in self._camera_keys:
            frame = self._extract_camera_frame(key, camera_group, robot_data, payload)
            if frame is None:
                raise KeyError(f"Camera '{key}' not found in observation payload")
            camera_arrays[key] = frame

        state_vector = self._gather_state_vector(robot_data)

        for name, slice_obj in self._state_slices:
            chunk = state_vector[slice_obj]
            result[f"state.{name}"] = chunk.astype(np.float32)

        for name, array in camera_arrays.items():
            result[f"video.{name}"] = array

        return result

    def _gather_state_vector(self, robot_data: Mapping[str, Any]) -> np.ndarray:
        values: list[float] = []
        for key in self._state_keys:
            value = robot_data.get(key)
            if value is None:
                raise KeyError(f"State key '{key}' missing from robot observation")
            values.append(float(value))
        return np.asarray(values, dtype=np.float32)

    def _extract_camera_frame(
        self,
        name: str,
        camera_group: Mapping[str, Any],
        robot_data: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> np.ndarray | None:
        for source in (camera_group, robot_data, payload):
            if isinstance(source, Mapping) and name in source:
                return self._coerce_frame(source[name])
        return None

    @staticmethod
    def _coerce_frame(value: Any) -> np.ndarray | None:
        try:
            array = np.asarray(value)
        except Exception:
            return None
        if array.ndim == 0:
            return None
        if array.ndim == 2:  # grayscale image
            array = array[:, :, None]
        if array.ndim == 3:
            array = array[None, ...]
        if array.ndim != 4:
            return None
        if array.dtype != np.uint8:
            if np.issubdtype(array.dtype, np.floating):
                scaled = array if array.max() > 1.0 else array * 255.0
                array = np.clip(scaled, 0, 255).astype(np.uint8)
            else:
                array = np.clip(array, 0, 255).astype(np.uint8)
        return array


__all__ = ["Gr00TObservationMapper"]
