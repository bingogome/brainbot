
from __future__ import annotations

from abc import ABC, abstractmethod

import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Mapping

from brainbot_core.config import DataModeConfig, TeleopEndpointConfig
from brainbot_core.transport import ActionInferenceClient, BaseZMQClient
import numpy as np
import zmq

try:
    from lerobot.processor import RobotProcessorPipeline, make_default_processors
except ImportError:  # compatibility with newer LeRobot releases
    from lerobot.processor.factory import make_default_processors  # type: ignore

    RobotProcessorPipeline = Any  # type: ignore
from lerobot.teleoperators.teleoperator import Teleoperator
from lerobot.teleoperators.utils import make_teleoperator_from_config
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from lerobot.datasets.pipeline_features import aggregate_pipeline_dataset_features, create_initial_features
from lerobot.datasets.utils import build_dataset_frame, combine_feature_dicts
from lerobot.datasets.video_utils import VideoEncodingManager
from lerobot.robots import Robot, make_robot_from_config
from lerobot.utils.constants import ACTION, OBS_STR
from lerobot.utils.control_utils import sanity_check_dataset_name, sanity_check_dataset_robot_compatibility
from lerobot.utils.import_utils import register_third_party_devices
from lerobot.utils.visualization_utils import init_rerun, log_rerun_data

from brainbot_core.proto import ActionMessage, MessageSerializer, ObservationMessage

logger = logging.getLogger(__name__)


def _numeric_only(values: dict[str, Any]) -> dict[str, float]:
    numeric: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, (int, float)):
            numeric[key] = float(value)
    return numeric


def _numeric_observation_payload(observation: ObservationMessage) -> dict[str, Any]:
    serialized = MessageSerializer.to_dict(observation)
    payload = serialized.get("payload", {})
    trimmed: dict[str, Any] = {}
    if isinstance(payload, Mapping):
        robot_raw = payload.get("robot", {})
        base_raw = payload.get("base", {})
        trimmed["robot"] = _numeric_only(robot_raw) if isinstance(robot_raw, Mapping) else {}
        trimmed["base"] = _numeric_only(base_raw) if isinstance(base_raw, Mapping) else {}
        for key, value in payload.items():
            if key in {"robot", "base"}:
                continue
            if isinstance(value, (int, float)):
                trimmed[key] = float(value)
    else:
        trimmed["robot"] = {}
        trimmed["base"] = {}
    trimmed["timestamp_ns"] = serialized.get("timestamp_ns", observation.timestamp_ns)
    metadata = serialized.get("metadata")
    if isinstance(metadata, Mapping):
        trimmed["metadata"] = {
            key: value for key, value in metadata.items() if isinstance(value, (int, float, str))
        }
    return trimmed


class CommandProvider(ABC):
    def prepare(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    @abstractmethod
    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        ...

    def wants_full_observation(self) -> bool:
        return False


class IdleCommandProvider(CommandProvider):
    def __init__(self, actions: dict[str, float] | None = None):
        self._actions = actions or {}

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        return ActionMessage(actions=dict(self._actions))


class AICommandProvider(CommandProvider):
    def __init__(
        self,
        client: ActionInferenceClient,
        instruction_key: str = "language_instruction",
        observation_adapter: Callable[[ObservationMessage], dict[str, Any]] | None = None,
        action_adapter: Callable[[dict[str, Any], int], list[dict[str, float]]] | None = None,
        action_horizon: int = 1,
    ):
        self.client = client
        self.instruction_key = instruction_key
        self._instruction: str | None = None
        self._observation_adapter = observation_adapter or (lambda obs: dict(obs.payload))
        self._action_adapter = action_adapter or _default_action_sequence
        self._action_horizon = max(1, int(action_horizon))
        self._pending_actions: deque[ActionMessage] = deque()

    def set_instruction(self, instruction: str) -> None:
        self._instruction = instruction
        self._pending_actions.clear()
        print(f"[ai] instruction set to: {instruction}")

    def clear_instruction(self) -> None:
        self._instruction = None
        self._pending_actions.clear()
        print("[ai] instruction cleared")

    def wants_full_observation(self) -> bool:
        return True

    def prepare(self) -> None:
        self._pending_actions.clear()

    def shutdown(self) -> None:
        self._pending_actions.clear()

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        if not self._instruction:
            self._pending_actions.clear()
            return ActionMessage(actions={})

        if not self._pending_actions:
            profile_start = time.perf_counter()
            obs_payload = self._observation_adapter(observation)
            encode_start = time.perf_counter()
            obs_payload[self.instruction_key] = self._instruction
            desc = obs_payload.get("annotation.human.task_description", self._instruction)
            if isinstance(desc, (list, tuple)):
                obs_payload["annotation.human.task_description"] = list(desc)
            else:
                obs_payload["annotation.human.task_description"] = [desc]
            for key, value in list(obs_payload.items()):
                if isinstance(value, np.ndarray):
                    continue
                if key.startswith("state.") and isinstance(value, list):
                    continue
                if isinstance(value, (list, tuple)):
                    continue
                obs_payload[key] = [value]
            encode_elapsed = time.perf_counter() - encode_start
            logger.debug("[ai-profile] encode %.3f ms", encode_elapsed * 1000.0)

            infer_start = time.perf_counter()
            try:
                action_chunk = self.client.get_action(obs_payload)
            except TimeoutError:
                infer_elapsed = time.perf_counter() - infer_start
                logger.warning("[ai] GR00T inference timed out after %.3f ms", infer_elapsed * 1000.0)
                raise
            except Exception as exc:
                infer_elapsed = time.perf_counter() - infer_start
                logger.error("[ai] inference error after %.3f ms: %s", infer_elapsed * 1000.0, exc)
                raise
            infer_elapsed = time.perf_counter() - infer_start
            logger.debug("[ai-profile] infer %.3f ms", infer_elapsed * 1000.0)
            logger.debug("[ai] received action keys: %s", list(action_chunk.keys()))

            adapt_start = time.perf_counter()
            try:
                batches = self._action_adapter(action_chunk, self._action_horizon)
            except Exception as exc:
                logger.error("[ai] failed to adapt action chunk: %s", exc)
                raise
            adapt_elapsed = time.perf_counter() - adapt_start
            total_elapsed = time.perf_counter() - profile_start
            logger.debug(
                "[ai-profile] encode=%.3fms infer=%.3fms adapt=%.3fms total=%.3fms",
                encode_elapsed * 1000.0,
                infer_elapsed * 1000.0,
                adapt_elapsed * 1000.0,
                total_elapsed * 1000.0,
            )

            if not batches:
                logger.warning("[ai] action adapter returned no actions; inserting noop")
                batches = [{}]
            for batch in batches:
                self._pending_actions.append(ActionMessage(actions=dict(batch)))

        if not self._pending_actions:
            return ActionMessage(actions={})
        return self._pending_actions.popleft()


def _default_action_sequence(values: dict[str, Any], _: int) -> list[dict[str, float]]:
    numeric = _numeric_only(values)
    return [numeric] if numeric else [{}]


class DataCollectionCommandProvider(CommandProvider):
    def __init__(self, config: DataModeConfig):
        self._config = config
        self._teleop_endpoint: TeleopEndpointConfig | None = config.teleop
        self._teleop: Teleoperator | None = None
        self._remote_provider: "RemoteTeleopCommandProvider | None" = None
        self._dataset_cfg = config.dataset
        self._teleop_action_processor: RobotProcessorPipeline | None = None
        self._robot_action_processor: RobotProcessorPipeline | None = None
        self._robot_observation_processor: RobotProcessorPipeline | None = None
        self._dataset: LeRobotDataset | None = None
        self._dataset_features: dict[str, dict] | None = None
        self._video_manager: VideoEncodingManager | None = None
        self._video_context_active = False
        self._spec_robot: Robot | None = None
        self._state: str = "idle"
        self._state_deadline: float | None = None
        self._episode_seconds = max(1e-3, float(config.dataset.episode_time_s))
        self._reset_seconds = max(0.0, float(config.dataset.reset_time_s))
        self._target_episodes = max(0, int(config.dataset.num_episodes))
        self._episodes_recorded = 0
        self._recording_enabled = False
        self._display_data = bool(config.display_data)
        self._complete_logged = False
        self._task = config.dataset.single_task

    def wants_full_observation(self) -> bool:
        return True

    def prepare(self) -> None:
        register_third_party_devices()
        teleop_endpoint = self._teleop_endpoint
        if teleop_endpoint is None:
            raise ValueError("Data mode requires a teleoperator configuration")

        self._teleop_action_processor, self._robot_action_processor, self._robot_observation_processor = (
            make_default_processors()
        )
        if teleop_endpoint.mode == "remote":
            if teleop_endpoint.remote is None:
                raise ValueError("Remote teleop configuration requires host/port settings")
            self._remote_provider = RemoteTeleopCommandProvider(
                host=teleop_endpoint.remote.host,
                port=teleop_endpoint.remote.port,
                timeout_ms=teleop_endpoint.remote.timeout_ms,
                api_token=teleop_endpoint.remote.api_token,
                observation_adapter=_numeric_observation_payload,
            )
            self._remote_provider.prepare()
        elif teleop_endpoint.mode == "local":
            if teleop_endpoint.local is None:
                raise ValueError("Local teleop configuration missing 'config' block")
            self._teleop = make_teleoperator_from_config(teleop_endpoint.local)
            self._teleop.connect()
        else:
            raise ValueError(f"Unsupported teleop mode '{teleop_endpoint.mode}' for data collection")
        self._spec_robot = make_robot_from_config(self._config.robot)
        self._dataset_cfg = self._config.dataset
        self._dataset_features = self._build_dataset_features(self._spec_robot)
        self._dataset = self._init_dataset(self._dataset_features)
        self._episodes_recorded = self._dataset.num_episodes

        self._video_manager = VideoEncodingManager(self._dataset)
        self._video_manager.__enter__()
        self._video_context_active = True

        if self._display_data:
            try:
                init_rerun(session_name="brainbot-data")
            except Exception as exc:  # pragma: no cover - optional dependency
                logger.warning("[data] failed to initialise rerun visualisation: %s", exc)
                self._display_data = False

        now = time.perf_counter()
        if self._target_episodes and self._episodes_recorded >= self._target_episodes:
            self._state = "complete"
            self._recording_enabled = False
            self._state_deadline = None
            if not self._complete_logged:
                logger.info(
                    "[data] dataset already has %s/%s episodes; teleop passthrough only",
                    self._episodes_recorded,
                    self._target_episodes,
                )
                self._complete_logged = True
        else:
            self._begin_recording(now, fresh=True)

    def shutdown(self) -> None:
        try:
            self._finalize_partial_episode()
        finally:
            self._recording_enabled = False
            if self._video_manager and self._video_context_active:
                try:
                    self._video_manager.__exit__(None, None, None)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("[data] failed to close video manager: %s", exc)
                self._video_context_active = False
            if self._dataset and self._dataset_cfg.push_to_hub:
                try:
                    self._dataset.push_to_hub(
                        tags=self._dataset_cfg.tags, private=self._dataset_cfg.private
                    )
                    logger.info("[data] dataset pushed to hub (%s)", self._dataset_cfg.repo_id)
                except Exception as exc:
                    logger.warning("[data] failed to push dataset to hub: %s", exc)
        if self._teleop is not None:
            try:
                self._teleop.disconnect()
            except Exception as exc:
                logger.warning("[data] teleop disconnect failed: %s", exc)
            self._teleop = None
        if self._remote_provider is not None:
            try:
                self._remote_provider.shutdown()
            except Exception as exc:
                logger.debug("[data] remote teleop shutdown encountered: %s", exc)
            self._remote_provider = None
            if self._spec_robot is not None:
                try:
                    self._spec_robot.disconnect()
                except Exception:
                    pass
                self._spec_robot = None
            self._dataset = None
            self._dataset_features = None
            self._video_manager = None
            self._state = "idle"
            self._state_deadline = None
            self._complete_logged = False

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        if self._dataset is None:
            raise RuntimeError("Data mode provider is not prepared")

        robot_obs = observation.payload.get("robot", {})
        if self._remote_provider is not None:
            action_msg = self._remote_provider.compute_command(observation)
            raw_action = dict(action_msg.actions)
        else:
            if self._teleop is None:
                raise RuntimeError("Teleoperator not available")
            raw_action = self._teleop.get_action()
        teleop_action = raw_action
        if self._teleop_action_processor:
            teleop_action = self._teleop_action_processor((raw_action, robot_obs))
        if not isinstance(teleop_action, dict):
            teleop_action = dict(teleop_action)

        robot_action = teleop_action
        if self._robot_action_processor:
            robot_action = self._robot_action_processor((teleop_action, robot_obs))
        if not isinstance(robot_action, dict):
            robot_action = dict(robot_action)

        if self._recording_enabled:
            if self._robot_observation_processor:
                obs_processed = self._robot_observation_processor(robot_obs)
            else:
                obs_processed = robot_obs
            if not isinstance(obs_processed, dict):
                obs_processed = dict(obs_processed)
            features = self._dataset.features
            observation_frame = build_dataset_frame(features, obs_processed, prefix=OBS_STR)
            action_frame = build_dataset_frame(features, teleop_action, prefix=ACTION)
            frame = {**observation_frame, **action_frame, "task": self._task}
            self._dataset.add_frame(frame)
            if self._display_data:
                try:
                    log_rerun_data(observation=obs_processed, action=teleop_action)
                except Exception as exc:
                    logger.debug("[data] rerun logging failed: %s", exc)

        now = time.perf_counter()
        self._update_state(now)

        return ActionMessage(actions=dict(robot_action))

    def _build_dataset_features(self, robot: Robot) -> dict[str, dict]:
        action_specs = getattr(robot, "action_features", {})
        obs_specs = getattr(robot, "observation_features", {})
        teleop_features = aggregate_pipeline_dataset_features(
            pipeline=self._teleop_action_processor,
            initial_features=create_initial_features(action=action_specs),
            use_videos=self._dataset_cfg.video,
        )
        observation_features = aggregate_pipeline_dataset_features(
            pipeline=self._robot_observation_processor,
            initial_features=create_initial_features(observation=obs_specs),
            use_videos=self._dataset_cfg.video,
        )
        return combine_feature_dicts(teleop_features, observation_features)

    def _init_dataset(self, features: dict[str, dict]) -> LeRobotDataset:
        cfg = self._dataset_cfg
        root = cfg.root
        camera_count = len(getattr(self._spec_robot, "cameras", {})) if self._spec_robot else 0
        writer_threads = cfg.num_image_writer_threads_per_camera * camera_count

        if self._config.resume:
            dataset = LeRobotDataset(
                cfg.repo_id,
                root=root,
                batch_encoding_size=cfg.video_encoding_batch_size,
            )
            if cfg.num_image_writer_processes or writer_threads:
                dataset.start_image_writer(cfg.num_image_writer_processes, writer_threads)
            try:
                if self._spec_robot:
                    sanity_check_dataset_robot_compatibility(dataset, self._spec_robot, cfg.fps, features)
            except Exception as exc:
                raise ValueError(f"Existing dataset metadata incompatible with current robot: {exc}") from exc
        else:
            sanity_check_dataset_name(cfg.repo_id, None)
            dataset = LeRobotDataset.create(
                repo_id=cfg.repo_id,
                fps=cfg.fps,
                features=features,
                root=root,
                robot_type=getattr(self._spec_robot, "name", None),
                use_videos=cfg.video,
                image_writer_processes=cfg.num_image_writer_processes,
                image_writer_threads=writer_threads,
                batch_encoding_size=cfg.video_encoding_batch_size,
            )
        return dataset

    def _begin_recording(self, now: float, *, fresh: bool = False) -> None:
        self._state = "record"
        self._recording_enabled = True
        self._state_deadline = now + self._episode_seconds
        current = self._episodes_recorded + 1
        target = self._target_episodes or "?"
        prefix = "Starting" if fresh else "Resuming"
        logger.info("[data] %s recording for episode %s/%s (%.1fs)", prefix, current, target, self._episode_seconds)

    def _enter_reset(self, now: float) -> None:
        self._state = "reset"
        self._recording_enabled = False
        self._state_deadline = now + self._reset_seconds
        logger.info("[data] reset window for %.1f seconds", self._reset_seconds)

    def _mark_complete(self) -> None:
        self._state = "complete"
        self._recording_enabled = False
        self._state_deadline = None
        if not self._complete_logged:
            logger.info(
                "[data] completed %s/%s episodes",
                self._episodes_recorded,
                self._target_episodes or self._episodes_recorded,
            )
            self._complete_logged = True

    def _finalize_episode(self) -> None:
        if not self._dataset:
            return
        if getattr(self._dataset, "episode_buffer", None):
            size = self._dataset.episode_buffer.get("size", 0)
            if not size:
                return
        self._dataset.save_episode()
        self._episodes_recorded = self._dataset.num_episodes
        logger.info("[data] saved episode %s", self._episodes_recorded)

    def _finalize_partial_episode(self) -> None:
        if not (self._dataset and self._recording_enabled):
            return
        buffer = getattr(self._dataset, "episode_buffer", None)
        if not buffer:
            return
        if buffer.get("size", 0):
            try:
                self._dataset.save_episode()
                self._episodes_recorded = self._dataset.num_episodes
                logger.info("[data] saved partial episode on shutdown")
            except Exception as exc:
                logger.warning("[data] failed to save partial episode: %s", exc)

    def _update_state(self, now: float) -> None:
        if self._state == "record" and self._state_deadline is not None and now >= self._state_deadline:
            self._finalize_episode()
            if self._target_episodes and self._episodes_recorded >= self._target_episodes:
                self._mark_complete()
                return
            if self._reset_seconds > 0:
                self._enter_reset(now)
            else:
                self._begin_recording(now)
        elif self._state == "reset" and self._state_deadline is not None and now >= self._state_deadline:
            self._begin_recording(now)

class LocalTeleopCommandProvider(CommandProvider):
    def __init__(
        self,
        teleop: Teleoperator,
        teleop_action_processor: RobotProcessorPipeline | None = None,
        robot_action_processor: RobotProcessorPipeline | None = None,
    ):
        self.teleop = teleop
        self.teleop_action_processor = teleop_action_processor
        self.robot_action_processor = robot_action_processor

    def prepare(self) -> None:
        self.teleop.connect()

    def shutdown(self) -> None:
        self.teleop.disconnect()

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        payload = observation.payload.get("robot", {})
        raw_action = self.teleop.get_action()
        teleop_action = raw_action
        if self.teleop_action_processor:
            teleop_action = self.teleop_action_processor((raw_action, payload))
        robot_action = teleop_action
        if self.robot_action_processor:
            robot_action = self.robot_action_processor((robot_action, payload))
        return ActionMessage(actions=dict(robot_action))


class RemoteTeleopClient(BaseZMQClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_timeouts()

    def _init_socket(self):
        super()._init_socket()
        self._apply_timeouts()

    def _apply_timeouts(self):
        if self.timeout_ms:
            self.socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
            self.socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)

    def get_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.call_endpoint("get_action", payload)


class RemoteTeleopCommandProvider(CommandProvider):
    def __init__(
        self,
        host: str,
        port: int,
        timeout_ms: int = 1500,
        api_token: str | None = None,
        observation_adapter: Callable[[ObservationMessage], dict[str, Any]] | None = None,
    ):
        self.host = host
        self.port = port
        self.timeout_ms = timeout_ms
        self.api_token = api_token
        self._client: RemoteTeleopClient | None = None
        self._observation_adapter = observation_adapter or (lambda obs: MessageSerializer.to_dict(obs))

    def prepare(self) -> None:
        if self._client is None:
            self._client = RemoteTeleopClient(
                host=self.host, port=self.port, timeout_ms=self.timeout_ms, api_token=self.api_token
            )
        else:
            self._client._init_socket()
        if not self._client.ping():
            raise ConnectionError(f"Failed to reach teleop server {self.host}:{self.port}")

    def shutdown(self) -> None:
        # Keep the remote connection alive; shutdown is a no-op to
        # avoid closing sockets that might still be in use by ZMQ.
        return None

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        if self._client is None:
            raise RuntimeError("Remote teleop client not connected")
        payload = self._observation_adapter(observation)
        if not isinstance(payload, dict):
            raise TypeError("Remote teleop observation adapter must return a dict")
        try:
            response = self._client.get_action({"observation": payload})
        except zmq.error.Again as exc:
            raise TimeoutError("Remote teleop timed out") from exc
        if "action" not in response:
            raise RuntimeError(f"Remote teleop response missing action: {response}")
        return MessageSerializer.ensure_action(response["action"])
