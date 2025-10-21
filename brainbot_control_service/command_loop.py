from __future__ import annotations

import logging
import threading
import time
from typing import Mapping

from brainbot_core.proto import ActionMessage

from .command_client import CommandChannelClient, ShutdownRequested
from .service import RobotControlService
from .camera_streamer import CameraStreamer


class CommandLoop:
    def __init__(
        self,
        service: RobotControlService,
        client: CommandChannelClient,
        rate_hz: float = 30.0,
        max_missed_actions: int = 3,
        fallback_action: Mapping[str, float] | None = None,
        camera_streamer: CameraStreamer | None = None,
    ):
        self.service = service
        self.client = client
        self.period = 1.0 / max(rate_hz, 1e-3)
        self.max_missed_actions = max_missed_actions
        self._missed_actions = 0
        self._running = threading.Event()
        self._running.set()
        self._fallback = ActionMessage(actions=dict(fallback_action or {}))
        self.camera_streamer = camera_streamer
        self._logger = logging.getLogger(__name__)

    def run(self) -> None:
        while self._running.is_set():
            loop_start = time.perf_counter()
            if self.camera_streamer:
                observation, raw_robot_obs = self.service.get_observation(return_raw=True)
                try:
                    self.camera_streamer.publish({"robot": raw_robot_obs})
                except Exception as exc:
                    print(f"[camera-stream] publish failed: {exc}")
            else:
                observation = self.service.get_observation()
            try:
                action = self.client.compute_action(observation)
            except ShutdownRequested:
                print("[command-loop] shutdown requested by command service")
                self.stop()
                break
            except TimeoutError:
                self._missed_actions += 1
                if self._missed_actions > self.max_missed_actions:
                    self._logger.warning("[command-loop] max missed actions reached; zeroing command")
                    action = self.service.zero_command()
                    self._missed_actions = 0
                elif self._fallback.actions:
                    self._logger.warning("[command-loop] using explicit fallback action")
                    action = ActionMessage(actions=dict(self._fallback.actions))
                else:
                    self._logger.warning("[command-loop] reusing last action as fallback")
                    action = self.service.fallback_command()
            else:
                self.service.set_observation_mode(self.client.last_observation_hint())
                self._missed_actions = 0
                self._logger.info("[command-loop] applying action keys: %s", list(action.actions.keys()))
            self.service.apply_action(action)
            remaining = self.period - (time.perf_counter() - loop_start)
            if remaining > 0:
                time.sleep(remaining)

    def stop(self) -> None:
        self._running.clear()
        if self.camera_streamer:
            self.camera_streamer.close()
