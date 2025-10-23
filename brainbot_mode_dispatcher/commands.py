from __future__ import annotations

import queue
from typing import Any, Mapping

from .events import (
    DataModeEvent,
    IdleModeEvent,
    InferenceModeEvent,
    ModeEvent,
    ShutdownModeEvent,
    TeleopModeEvent,
)


def enqueue_mode_command(data: Mapping[str, Any], queue_obj: queue.Queue[ModeEvent]) -> bool:
    """Translate a JSON-compatible dict into one or more mode events."""
    dispatched = False
    if "data" in data:
        value = data["data"]
        if isinstance(value, Mapping):
            target = value.get("mode")
            command = value.get("command")
            if target is not None:
                queue_obj.put(TeleopModeEvent(alias=str(target) if target else "data"))
                dispatched = True
            if command:
                queue_obj.put(DataModeEvent(command=str(command)))
                dispatched = True
        else:
            if value in (None, ""):
                queue_obj.put(TeleopModeEvent(alias="data"))
            else:
                queue_obj.put(DataModeEvent(command=str(value)))
            dispatched = True
        return dispatched
    if "teleop" in data:
        queue_obj.put(TeleopModeEvent(alias=str(data["teleop"])))
        return True
    if "infer" in data:
        queue_obj.put(InferenceModeEvent(instruction=str(data["infer"]).strip()))
        return True
    if "idle" in data:
        value = data["idle"]
        queue_obj.put(IdleModeEvent(reason=str(value)) if value else IdleModeEvent())
        return True
    if "shutdown" in data:
        value = data["shutdown"]
        queue_obj.put(ShutdownModeEvent(reason=str(value)) if value else ShutdownModeEvent())
        return True
    return False
