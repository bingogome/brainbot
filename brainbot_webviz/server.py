from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import msgpack
import numpy as np
import zmq

try:
    import cv2
except ImportError:  # pragma: no cover - optional dependency
    cv2 = None  # type: ignore[assignment]


class VisualizationServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        camera_host: str | None = None,
        camera_port: int | None = None,
    ):
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "observation": {},
            "action": {},
            "timestamp": 0.0,
            "mode": "unknown",
            "history": [],
            "previews": {},
        }
        self._history: list[dict[str, Any]] = []
        self._camera_lock = threading.Lock()
        self._camera_frames: dict[str, dict[str, Any]] = {}
        self._camera_subscriber: CameraSubscriber | None = None
        self._server = ThreadingHTTPServer((self.host, self.port), self._handler_factory())
        self._thread: threading.Thread | None = None

        if camera_host and camera_port:
            self._camera_subscriber = CameraSubscriber(
                host=camera_host,
                port=camera_port,
                callback=self._on_camera_frame,
            )

    def _handler_factory(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/data":
                    with outer._lock:
                        payload = json.dumps(outer._data).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(_DASHBOARD_HTML.encode("utf-8"))

            def log_message(self, format, *args):  # noqa: A003
                return

        return Handler

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._camera_subscriber:
            self._camera_subscriber.stop()

    def update(self, observation: dict[str, Any], action: dict[str, Any], mode: str) -> None:
        observation_snapshot = _summarize_payload(observation)
        clean_action = _sanitize_payload(action)

        numeric_values = _extract_numeric(action)
        entry = {"timestamp": time.time(), "values": numeric_values}
        if numeric_values:
            self._history.append(entry)
            if len(self._history) > 200:
                self._history = self._history[-200:]

        history_snapshot = [
            {"timestamp": item["timestamp"], "values": dict(item["values"])}
            for item in self._history
        ]

        previews = self._snapshot_camera_frames()
        inline_previews = _extract_inline_previews(observation)
        if inline_previews:
            merged: dict[str, Any] = dict(inline_previews)
            merged.update(previews)
            previews = merged

        with self._lock:
            self._data = {
                "observation": observation_snapshot,
                "action": clean_action,
                "timestamp": entry["timestamp"],
                "mode": mode,
                "history": history_snapshot,
                "previews": previews,
            }

    def _snapshot_camera_frames(self) -> dict[str, Any]:
        with self._camera_lock:
            return {name: dict(frame) for name, frame in self._camera_frames.items()}

    def _on_camera_frame(self, name: str, payload: dict[str, Any]) -> None:
        data = payload.get("data")
        if not isinstance(data, (bytes, bytearray)):
            return
        encoded = base64.b64encode(data).decode("ascii")
        frame_info = {
            "camera": name,
            "timestamp": float(payload.get("timestamp", time.time())),
            "width": int(payload.get("width", 0)),
            "height": int(payload.get("height", 0)),
            "src": f"data:image/jpeg;base64,{encoded}",
        }
        with self._camera_lock:
            self._camera_frames[name] = frame_info


def _sanitize_payload(obj: Any, prefix: str | None = None) -> Any:
    name_prefix = prefix or ""
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for key, value in obj.items():
            if key in {"message_type", "timestamp_ns", "version"}:
                continue
            child_prefix = f"{name_prefix}.{key}" if name_prefix else key
            result[key] = _sanitize_payload(value, child_prefix)
        return result
    if isinstance(obj, list):
        if len(obj) > 128:
            return [_sanitize_payload(v, name_prefix) for v in obj[:128]] + ["..."]
        return [_sanitize_payload(v, name_prefix) for v in obj]
    return obj


def _extract_numeric(action: dict[str, Any]) -> dict[str, float]:
    numeric: dict[str, float] = {}
    values = action.get("actions", action)
    if isinstance(values, dict):
        iterator = values.items()
    else:
        iterator = action.items()
    for key, value in iterator:
        try:
            numeric[key] = float(value)
        except (TypeError, ValueError):
            continue
    return numeric


def _extract_inline_previews(observation: Any) -> dict[str, Any]:
    frames: dict[str, Any] = {}
    if not isinstance(observation, Mapping):
        return frames

    def _walk(node: Any, prefix: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                name = f"{prefix}.{key}" if prefix else str(key)
                _walk(value, name)
            return
        if isinstance(node, (list, tuple)):
            for idx, value in enumerate(node):
                name = f"{prefix}[{idx}]"
                _walk(value, name)
            return
        if isinstance(node, np.ndarray):
            label = prefix or "observation"
            frame_info = _encode_inline_frame(label, node)
            if frame_info:
                frames[label] = frame_info

    _walk(observation, "")
    return frames


def _encode_inline_frame(name: str, frame: np.ndarray) -> dict[str, Any] | None:
    if cv2 is None:
        return None
    image = np.asarray(frame)
    if image.ndim not in (2, 3):
        return None
    if image.ndim == 3 and image.shape[2] not in (1, 3, 4):
        return None
    height, width = (image.shape[:2] if image.ndim >= 2 else (0, 0))
    if height < 32 or width < 32:
        return None
    if image.dtype != np.uint8:
        if np.issubdtype(image.dtype, np.floating):
            scaled = image
            if scaled.max() <= 1.0:
                scaled = scaled * 255.0
            image = np.clip(scaled, 0, 255).astype(np.uint8)
        else:
            image = np.clip(image, 0, 255).astype(np.uint8)
    if image.ndim == 3:
        if image.shape[2] == 1:
            image = np.squeeze(image, axis=2)
        elif image.shape[2] == 4:
            image = image[..., :3]
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    success, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
    if not success:
        return None
    encoded = base64.b64encode(buffer).decode("ascii")
    return {
        "camera": name or "observation",
        "timestamp": time.time(),
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
        "src": f"data:image/jpeg;base64,{encoded}",
    }


def _summarize_payload(obj: Any, depth: int = 0) -> Any:
    if depth >= 3:
        return "..."
    if isinstance(obj, np.ndarray):
        return {
            "type": "ndarray",
            "shape": list(obj.shape),
            "dtype": str(obj.dtype),
        }
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, Mapping):
        return {str(key): _summarize_payload(value, depth + 1) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        max_items = 8
        items = [_summarize_payload(value, depth + 1) for value in obj[:max_items]]
        if len(obj) > max_items:
            items.append("...")
        return items
    if isinstance(obj, (bytes, bytearray)):
        return f"<bytes:{len(obj)}>"
    return obj


_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Brainbot Telemetry</title>
  <style>
    body { font-family: sans-serif; margin: 2rem; }
    pre { background: #f5f5f5; padding: 1rem; border-radius: 6px; }
    #chart-container { width: 100%; max-width: 960px; margin-bottom: 2rem; }
    .image-grid { display: flex; flex-wrap: wrap; gap: 1rem; margin-top: 2rem; }
    .image-grid figure { margin: 0; }
    .image-grid img { max-width: 320px; border-radius: 6px; box-shadow: 0 2px 6px rgba(0,0,0,0.2); }
    .image-grid figcaption { text-align: center; margin-top: 0.5rem; font-size: 0.9rem; }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
  <h1>Brainbot Command/Observation Feed</h1>
  <p>Mode: <strong id="mode">unknown</strong></p>
  <p>Latest timestamp: <span id="ts">n/a</span></p>
  <h2>Observation</h2>
  <pre id="obs"></pre>
  <h2>Action</h2>
  <pre id="act"></pre>
  <div id="chart-container">
    <canvas id="actionChart"></canvas>
  </div>
  <h2>Camera Previews</h2>
  <div class="image-grid" id="images"></div>
  <script>
    const colors = [
      '#e6194b', '#3cb44b', '#ffe119', '#4363d8', '#f58231',
      '#911eb4', '#46f0f0', '#f032e6', '#bcf60c', '#fabebe',
      '#008080', '#e6beff', '#9a6324', '#fffac8', '#800000',
      '#aaffc3', '#808000', '#ffd8b1', '#000075', '#808080'
    ];
    let actionChart = null;
    let chartKeys = [];

    async function refresh() {
      try {
        const res = await fetch('/data');
        if (!res.ok) return;
        const data = await res.json();
        document.getElementById('mode').textContent = data.mode || 'unknown';
        document.getElementById('ts').textContent = new Date(data.timestamp * 1000).toLocaleString();
        document.getElementById('obs').textContent = JSON.stringify(data.observation, null, 2);
        document.getElementById('act').textContent = JSON.stringify(data.action, null, 2);
        updateChart(data.history || []);
        updateImages(data.previews || {});
      } catch (err) {
        console.error('Refresh error', err);
      }
    }

    function updateImages(previews) {
      const container = document.getElementById('images');
      container.innerHTML = '';
      Object.keys(previews).forEach(name => {
        const frame = previews[name];
        if (!frame.src) return;
        const figure = document.createElement('figure');
        const img = document.createElement('img');
        img.src = frame.src;
        img.alt = name;
        const caption = document.createElement('figcaption');
        const ts = new Date(frame.timestamp * 1000).toLocaleTimeString();
        caption.textContent = `${name} (${frame.width}×${frame.height}) – ${ts}`;
        figure.appendChild(img);
        figure.appendChild(caption);
        container.appendChild(figure);
      });
    }

    function updateChart(history) {
      const labels = history.map(item => new Date(item.timestamp * 1000).toLocaleTimeString());
      const keys = Array.from(new Set(history.flatMap(item => Object.keys(item.values || {}))));

      if (!actionChart || JSON.stringify(keys) !== JSON.stringify(chartKeys)) {
        const datasets = keys.map((key, idx) => ({
          label: key,
          data: history.map(item => (item.values && key in item.values ? item.values[key] : null)),
          borderColor: colors[idx % colors.length],
          tension: 0.2,
          spanGaps: true,
          fill: false,
        }));
        const ctx = document.getElementById('actionChart').getContext('2d');
        if (actionChart) actionChart.destroy();
        actionChart = new Chart(ctx, {
          type: 'line',
          data: { labels, datasets },
          options: {
            responsive: true,
            scales: {
              x: { display: true, title: { display: true, text: 'Time' } },
              y: { display: true, title: { display: true, text: 'Value' } },
            },
            interaction: { mode: 'index', intersect: false },
          },
        });
        chartKeys = keys;
      } else {
        actionChart.data.labels = labels;
        actionChart.data.datasets.forEach((dataset, idx) => {
          const key = chartKeys[idx];
          dataset.data = history.map(item => (item.values && key in item.values ? item.values[key] : null));
        });
        actionChart.update();
      }
    }

    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>
"""


class CameraSubscriber:
    def __init__(self, host: str, port: int, callback: Callable[[str, dict[str, Any]], None]):
        self._callback = callback
        self._context = zmq.Context.instance()
        self._socket = self._context.socket(zmq.SUB)
        self._socket.connect(f"tcp://{host}:{port}")
        self._socket.setsockopt(zmq.SUBSCRIBE, b"")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        poller = zmq.Poller()
        poller.register(self._socket, zmq.POLLIN)
        while not self._stop.is_set():
            events = dict(poller.poll(500))
            if self._socket in events and events[self._socket] == zmq.POLLIN:
                try:
                    topic, payload = self._socket.recv_multipart()
                except ValueError:
                    continue
                try:
                    data = msgpack.unpackb(payload, raw=False)
                except Exception:
                    continue
                camera = topic.decode("utf-8")
                try:
                    self._callback(camera, data)
                except Exception:
                    continue

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._socket.close(0)
