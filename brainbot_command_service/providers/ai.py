from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np

from brainbot_core.transport import ActionInferenceClient
from brainbot_core.proto import ActionMessage, ObservationMessage

from .base import CommandProvider, numeric_only

logger = logging.getLogger(__name__)


def _default_action_sequence(values: dict[str, Any], _: int) -> list[dict[str, float]]:
    numeric = numeric_only(values)
    return [numeric] if numeric else [{}]


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
