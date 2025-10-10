from __future__ import annotations

import json
import queue
import threading

from .events import IdleModeEvent, InferenceModeEvent, ModeEventDispatcher, ModeEventListener, TeleopModeEvent


class CLIModeDispatcher(ModeEventDispatcher):
    def __init__(self, prompt: str = "> "):
        self._prompt = prompt
        self._queue: queue.Queue[IdleModeEvent | InferenceModeEvent | TeleopModeEvent] | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener: ModeEventListener | None = None
        self._dispatcher_thread: threading.Thread | None = None

    def start(self, listener: ModeEventListener) -> None:
        self._stop_event.clear()
        self._queue = queue.Queue()
        self._listener = listener
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        self._dispatcher_thread = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._dispatcher_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=0.1)
        if self._dispatcher_thread and self._dispatcher_thread.is_alive():
            self._dispatcher_thread.join(timeout=0.1)
        self._queue = None
        self._listener = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                raw = input(self._prompt)
            except EOFError:
                break
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[dispatcher] invalid JSON: {exc}")
                continue
            if not isinstance(data, dict):
                print("[dispatcher] command must be a JSON object")
                continue
            self._handle_command(data)

    def _dispatch_loop(self) -> None:
        queue_obj = self._queue
        listener = self._listener
        if queue_obj is None or listener is None:
            return
        while not self._stop_event.is_set():
            try:
                event = queue_obj.get(timeout=0.1)
            except queue.Empty:
                continue
            listener(event)

    def _handle_command(self, data: dict) -> None:
        queue_obj = self._queue
        if queue_obj is None:
            return
        if "teleop" in data:
            queue_obj.put(TeleopModeEvent(alias=str(data["teleop"])))
            return
        if "infer" in data:
            queue_obj.put(InferenceModeEvent(instruction=str(data["infer"]).strip()))
            return
        if "idle" in data:
            queue_obj.put(IdleModeEvent(reason=str(data["idle"])) if data["idle"] else IdleModeEvent())
            return
        print(f"[dispatcher] unsupported command: {data}")
