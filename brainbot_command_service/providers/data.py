from __future__ import annotations

import logging
import time
from typing import Any

try:
    from lerobot.processor import RobotProcessorPipeline, make_default_processors
except ImportError:  # compatibility with newer LeRobot releases
    from lerobot.processor.factory import make_default_processors  # type: ignore

    RobotProcessorPipeline = Any  # type: ignore

from lerobot.datasets.lerobot_dataset import LeRobotDataset
from lerobot.datasets.pipeline_features import aggregate_pipeline_dataset_features, create_initial_features
from lerobot.datasets.utils import build_dataset_frame, combine_feature_dicts
from lerobot.datasets.video_utils import VideoEncodingManager
from lerobot.robots import Robot, make_robot_from_config
from lerobot.teleoperators.teleoperator import Teleoperator
from lerobot.teleoperators.utils import make_teleoperator_from_config
from lerobot.utils.constants import ACTION, OBS_STR
from lerobot.utils.control_utils import sanity_check_dataset_name, sanity_check_dataset_robot_compatibility
from lerobot.utils.import_utils import register_third_party_devices
from lerobot.utils.utils import log_say
from lerobot.utils.visualization_utils import init_rerun, log_rerun_data

from brainbot_core.config import DataModeConfig, TeleopEndpointConfig
from brainbot_core.proto import ActionMessage, ObservationMessage

from .base import CommandProvider
from .teleop import RemoteTeleopCommandProvider, numeric_observation_payload

logger = logging.getLogger(__name__)


class DataCollectionCommandProvider(CommandProvider):
    def __init__(self, config: DataModeConfig):
        self._config = config
        self._teleop_endpoint: TeleopEndpointConfig | None = config.teleop
        self._teleop: Teleoperator | None = None
        self._remote_provider: RemoteTeleopCommandProvider | None = None
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
        self._events: dict[str, bool] = {
            "exit_early": False,
            "rerecord_episode": False,
            "stop_recording": False,
            "reset_requested": False,
            "continue_after_reset": False,
        }
        self._play_sounds = bool(config.play_sounds)

    def wants_full_observation(self) -> bool:
        return True

    def handle_control_command(self, command: str) -> None:
        command = command.strip().lower()
        events = self._events
        force_process = command in {"stop", "end", "finish"}
        if command in {"stop", "end", "finish"}:
            events["stop_recording"] = True
            logger.info("[data-control] stop command acknowledged")
            print("[data-control] stop command acknowledged")
        elif command in {"next", "skip"}:
            events["exit_early"] = True
            logger.info("[data-control] advance command acknowledged")
            print("[data-control] advance command acknowledged")
        elif command in {"rerecord", "redo"}:
            events["rerecord_episode"] = True
            events["exit_early"] = True
            logger.info("[data-control] rerecord command acknowledged")
            print("[data-control] rerecord command acknowledged")
        elif command in {"reset"}:
            events["reset_requested"] = True
            logger.info("[data-control] reset command acknowledged")
            print("[data-control] reset command acknowledged")
        elif command in {"resume", "next_stage"}:
            events["continue_after_reset"] = True
            logger.info("[data-control] continue command acknowledged")
            print("[data-control] continue command acknowledged")
        elif command == "start":
            logger.info("[data-control] start command acknowledged")
            print("[data-control] start command acknowledged")
        else:
            logger.warning("[data-control] unknown command: %s", command)
            print(f"[data-control] unknown command: {command}")
        self._process_events(force=force_process)

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
                observation_adapter=numeric_observation_payload,
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
        self._spec_robot = None
        self._dataset = None
        self._dataset_features = None
        self._video_manager = None
        self._state = "idle"
        self._state_deadline = None
        self._complete_logged = False
        log_say("Exiting", self._play_sounds)

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        if self._dataset is None:
            raise RuntimeError("Data mode provider is not prepared")

        robot_obs = observation.payload.get("robot", {})
        buffer_size = getattr(self._dataset, "episode_buffer", {}).get("size", 0)
        logger.debug(
            "[data] compute_command state=%s record=%s buffer=%s", self._state, self._recording_enabled, buffer_size
        )
        if buffer_size % 100 == 0:
            print(f"[data] state={self._state} record={self._recording_enabled} buffer={buffer_size}")
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
        keys = list(teleop_action.keys())
        logger.debug("[data] teleop action keys: %s", keys)
        if buffer_size % 100 == 0:
            print(f"[data] teleop action keys: {keys}")

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
            frame_size = getattr(self._dataset, "episode_buffer", {}).get("size", 0)
            logger.debug("[data] buffered frame count: %s", frame_size)
            if frame_size % 100 == 0:
                print(f"[data] buffered frame count: {frame_size}")
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
        print(f"[data-state] begin_recording episode={current}/{target}")
        if self._dataset is not None:
            log_say(f"Recording episode {self._dataset.num_episodes}", self._play_sounds)

    def _enter_reset(self, now: float) -> None:
        self._state = "reset"
        self._recording_enabled = False
        self._state_deadline = now + self._reset_seconds
        logger.info("[data] reset window for %.1f seconds", self._reset_seconds)
        print(f"[data-state] enter_reset duration={self._reset_seconds}s")
        log_say("Reset the environment", self._play_sounds)

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
        log_say("Stop recording", self._play_sounds, blocking=True)
        print("[data-state] recording complete")
        if self._events:
            self._events["exit_early"] = False
            self._events["rerecord_episode"] = False
            self._events["stop_recording"] = False
            self._events["reset_requested"] = False
            self._events["continue_after_reset"] = False
            self._events["continue_after_reset"] = False

    def _finalize_episode(self) -> None:
        if not self._dataset:
            return
        print("[data] _finalize_episode invoked")
        if getattr(self._dataset, "episode_buffer", None):
            size = self._dataset.episode_buffer.get("size", 0)
            if not size:
                logger.debug("[data] no frames to finalize, skipping save")
                print("[data] finalize skipped; no frames")
                return
            else:
                logger.info("[data] finalizing episode with %s frames", size)
                print(f"[data] finalizing episode with {size} frames")
        self._dataset.save_episode()
        self._episodes_recorded = self._dataset.num_episodes
        logger.info("[data] saved episode %s", self._episodes_recorded)
        print(f"[data] saved episode {self._episodes_recorded}")

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

    def _process_events(self, force: bool = False) -> None:
        print(f"[data] processing events force={force}")
        self._update_state(time.perf_counter(), force=force)

    def _update_state(self, now: float, force: bool = False) -> None:
        events = self._events or {}
        logger.debug(
            "[data-state] state=%s force=%s events=%s deadline=%s",
            self._state,
            force,
            {k: v for k, v in events.items() if v},
            self._state_deadline,
        )
        active = {k: v for k, v in events.items() if v}
        if active or force:
            print(f"[data-state] state={self._state} force={force} events={active} deadline={self._state_deadline}")

        if events.get("stop_recording") and self._state != "complete":
            if self._state in {"record", "reset"}:
                print("[data] stop requested; finalizing current episode if needed")
                self._finalize_episode()
            events["stop_recording"] = False
            events["exit_early"] = False
            events["rerecord_episode"] = False
            events["reset_requested"] = False
            self._mark_complete()
            return

        reset_requested = events.get("reset_requested", False)
        continue_after_reset = events.get("continue_after_reset", False)
        if reset_requested and not continue_after_reset:
            events["reset_requested"] = False
            if self._state == "record":
                self._finalize_episode()
                if self._target_episodes and self._episodes_recorded >= self._target_episodes:
                    self._mark_complete()
                    return
                self._enter_reset(now)
                return
            if self._state == "reset":
                self._begin_recording(now)
                return

        if continue_after_reset:
            events["continue_after_reset"] = False
            events["reset_requested"] = False
            if self._state == "reset":
                print("[data-state] continue_after_reset -> begin_recording")
                self._begin_recording(now)
                return

        if self._state == "record":
            deadline_reached = self._state_deadline is not None and now >= self._state_deadline
            exit_requested = events.get("exit_early", False) or force
            if deadline_reached or exit_requested:
                self._finalize_episode()
                if events.get("rerecord_episode"):
                    events["rerecord_episode"] = False
                    events["exit_early"] = False
                    if self._dataset is not None:
                        self._dataset.clear_episode_buffer()
                    logger.info("[data] re-recording current episode on user request")
                    log_say("Re-record episode", self._play_sounds)
                    self._begin_recording(now)
                    return
                events["exit_early"] = False
                if self._target_episodes and self._episodes_recorded >= self._target_episodes:
                    self._mark_complete()
                    return
                if events.get("stop_recording"):
                    events["stop_recording"] = False
                    self._mark_complete()
                    return
                if self._reset_seconds > 0 and not force:
                    self._enter_reset(now)
                else:
                    self._begin_recording(now)
        elif self._state == "reset":
            deadline_reached = self._state_deadline is not None and now >= self._state_deadline
            exit_requested = events.get("exit_early", False) or events.get("stop_recording", False) or force
            if deadline_reached or exit_requested:
                events["exit_early"] = False
                if events.get("stop_recording"):
                    events["stop_recording"] = False
                    self._mark_complete()
                    return
                if self._target_episodes and self._episodes_recorded >= self._target_episodes:
                    self._mark_complete()
                else:
                    self._begin_recording(now)
