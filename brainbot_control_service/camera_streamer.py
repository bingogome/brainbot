from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass
import queue
from typing import Any, Callable

import cv2
import msgpack
import numpy as np
import zmq

from brainbot_core.config import CameraStreamConfig, CameraStreamSourceConfig


@dataclass
class _CameraWorker:
    name: str
    path: str
    topic: bytes
    min_interval: float
    quality: int
    enqueue: Callable[[tuple[bytes, bytes]], None]

    def __post_init__(self) -> None:
        self._latest = collections.deque(maxlen=1)
        self._event = threading.Event()
        self._stop = threading.Event()
        self._last_emit = 0.0
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def submit(self, frame: np.ndarray) -> None:
        self._latest.append(frame)
        self._event.set()

    def stop(self) -> None:
        self._stop.set()
        self._event.set()
        self._thread.join(timeout=1.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._event.wait(timeout=0.5)
            if self._stop.is_set():
                break
            frame = None
            if self._latest:
                frame = self._latest.pop()
                self._latest.clear()
            self._event.clear()
            if frame is None:
                continue
            payload = _encode_frame(
                frame,
                self.name,
                time.time(),
                self.quality,
            )
            if payload is None:
                continue
            message = msgpack.packb(payload, use_bin_type=True)
            self.enqueue((self.topic, message))
            self._last_emit = float(payload.get("timestamp", time.time()))


def _encode_frame(frame: np.ndarray, name: str, timestamp: float, quality: int) -> dict[str, Any] | None:
    image = np.asarray(frame)
    if image.dtype != np.uint8:
        image = np.clip(image, 0, 255).astype(np.uint8)
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    elif image.ndim == 3 and image.shape[2] == 3:
        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    success, buffer = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not success:
        return None
    return {
        "camera": name,
        "timestamp": timestamp,
        "encoding": "jpeg",
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
        "quality": int(quality),
        "data": buffer.tobytes(),
    }


def _get_nested(payload: Any, path: str) -> Any:
    current = payload
    parts = path.split('.')
    index = 0
    while index < len(parts):
        part = parts[index]
        if isinstance(current, dict):
            if part in current:
                current = current[part]
                index += 1
                continue
            remaining = ".".join(parts[index:])
            if remaining and remaining in current:
                return current[remaining]
            if index + 1 < len(parts) and parts[index + 1] in current:
                index += 1
                continue
        return None
    return current


class CameraStreamer:
    def __init__(self, config: CameraStreamConfig):
        self.config = config
        self._context = zmq.Context.instance()
        self._socket = self._context.socket(zmq.PUB)
        self._socket.bind(f"tcp://{config.host}:{config.port}")
        self._queue: queue.Queue[tuple[bytes, bytes]] = queue.Queue()
        self._stop = threading.Event()
        self._publisher_thread = threading.Thread(target=self._publisher_loop, daemon=True)
        self._publisher_thread.start()
        self._workers: list[_CameraWorker] = []
        for source_cfg in config.sources:
            min_interval = 1.0 / source_cfg.fps if source_cfg.fps else 0.0
            quality = source_cfg.quality if source_cfg.quality is not None else config.quality
            topic = source_cfg.name.encode("utf-8")
            worker = _CameraWorker(
                name=source_cfg.name,
                path=source_cfg.path,
                topic=topic,
                min_interval=min_interval,
                quality=quality,
                enqueue=self._queue.put,
            )
            self._workers.append(worker)

    def _publisher_loop(self) -> None:
        while not self._stop.is_set():
            try:
                topic, message = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._socket.send_multipart([topic, message], zmq.NOBLOCK)
            except zmq.Again:
                continue

    def publish(self, observation: dict[str, Any]) -> None:
        if not self._workers:
            return
        now = time.time()
        for worker in self._workers:
            frame = _get_nested(observation, worker.path)
            if frame is None:
                continue
            if worker.min_interval and now - worker._last_emit < worker.min_interval:
                continue
            if not isinstance(frame, np.ndarray):
                frame = np.asarray(frame)
            worker.submit(frame)

    def close(self) -> None:
        self._stop.set()
        for worker in self._workers:
            worker.stop()
        self._workers.clear()
        if getattr(self, "_socket", None) is not None:
            self._socket.close(0)
            self._socket = None
        if getattr(self, "_publisher_thread", None):
            self._publisher_thread.join(timeout=1.0)
            self._publisher_thread = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
