from __future__ import annotations

import json
import threading

from .events import IdleModeEvent, InferenceModeEvent, ModeEventDispatcher, ModeEventListener, TeleopModeEvent


class KeyboardModeDispatcher(ModeEventDispatcher):
    def __init__(self, prompt: str = "> "):
        self._prompt = prompt
        self._listener: ModeEventListener | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, listener: ModeEventListener) -> None:
        self._listener = listener
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=0.1)

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

    def _handle_command(self, data: dict) -> None:
        listener = self._listener
        if listener is None:
            return
        if "teleop" in data:
            listener(TeleopModeEvent(alias=str(data["teleop"])))
            return
        if "infer" in data:
            listener(InferenceModeEvent(instruction=str(data["infer"]).strip()))
            return
        if "idle" in data:
            listener(IdleModeEvent(reason=str(data["idle"])) if data["idle"] else IdleModeEvent())
            return
        print(f"[dispatcher] unsupported command: {data}")
