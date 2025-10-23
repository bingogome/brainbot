from __future__ import annotations

from typing import Any
import logging

try:
    from lerobot.processor import RobotProcessorPipeline, make_default_processors
except ImportError:  # compatibility with newer LeRobot releases
    from lerobot.processor.factory import make_default_processors  # type: ignore

    RobotProcessorPipeline = Any  # type: ignore
from lerobot.teleoperators.teleoperator import Teleoperator

from brainbot_core.transport import BaseZMQServer
from brainbot_core.proto import ActionMessage, MessageSerializer


class TeleopActionServer(BaseZMQServer):
    _logger = logging.getLogger(__name__)
    def __init__(
        self,
        teleop: Teleoperator,
        host: str = "0.0.0.0",
        port: int = 7001,
        api_token: str | None = None,
        teleop_action_processor: RobotProcessorPipeline | None = None,
        robot_action_processor: RobotProcessorPipeline | None = None,
    ):
        super().__init__(host=host, port=port, api_token=api_token)
        self.teleop = teleop
        if teleop_action_processor is None or robot_action_processor is None:
            default_teleop, default_robot, _ = make_default_processors()
            self.teleop_action_processor = teleop_action_processor or default_teleop
            self.robot_action_processor = robot_action_processor or default_robot
        else:
            self.teleop_action_processor = teleop_action_processor
            self.robot_action_processor = robot_action_processor
        self.register_endpoint("get_action", self._handle_get_action)
        self.register_endpoint("sync_config", self._handle_sync_config)

    def run(self) -> None:
        self.teleop.connect()
        try:
            try:
                super().run()
            except KeyboardInterrupt:
                pass
        finally:
            try:
                self.teleop.disconnect()
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.warning("Teleop disconnect failed (%s): %s", exc.__class__.__name__, exc)
            self.close()

    def _handle_get_action(self, data: dict[str, Any]) -> dict[str, Any]:
        obs = data.get("observation", {})
        robot_obs = obs.get("robot", {}) if isinstance(obs, dict) else {}
        if hasattr(self.teleop, "on_observation"):
            try:
                self.teleop.on_observation(robot_obs)
            except Exception:
                pass
        raw_action = self.teleop.get_action()
        teleop_action = (
            self.teleop_action_processor((raw_action, robot_obs))
            if self.teleop_action_processor
            else raw_action
        )
        robot_action = (
            self.robot_action_processor((teleop_action, robot_obs))
            if self.robot_action_processor
            else teleop_action
        )
        message = MessageSerializer.to_dict(ActionMessage(actions=dict(robot_action)))
        return {"action": message}

    def _handle_sync_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "teleop_id": getattr(self.teleop, "id", "unknown")}
