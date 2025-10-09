from __future__ import annotations

import argparse
from pathlib import Path

from lerobot.processor import make_default_processors
from lerobot.teleoperators.utils import make_teleoperator_from_config

from gr00t.eval.service import ExternalRobotInferenceClient
from brainbot_core.config import ServerRuntimeConfig, load_server_config
from brainbot_mode_dispatcher import KeyboardModeDispatcher

from . import AICommandProvider, CommandProvider, CommandService, IdleCommandProvider, TeleopCommandProvider
from .mode_manager import ModeManager


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()

    config: ServerRuntimeConfig = load_server_config(args.config)
    teleop_providers: dict[str, TeleopCommandProvider] = {}
    teleop_aliases: dict[str, str] = {}
    for name, teleop_cfg in config.teleops.items():
        teleop = make_teleoperator_from_config(teleop_cfg)
        teleop_action_processor, robot_action_processor, _ = make_default_processors()
        key = f"teleop:{name}"
        teleop_providers[key] = TeleopCommandProvider(
            teleop=teleop,
            teleop_action_processor=teleop_action_processor,
            robot_action_processor=robot_action_processor,
        )
        teleop_aliases[name] = key
        teleop_aliases[key] = key

    providers: dict[str, CommandProvider] = dict(teleop_providers)
    ai_key: str | None = None
    if config.ai:
        ai_client = ExternalRobotInferenceClient(
            host=config.ai.host,
            port=config.ai.port,
            timeout_ms=config.ai.timeout_ms,
            api_token=config.ai.api_token,
        )
        ai_provider = AICommandProvider(
            client=ai_client,
            instruction_key=config.ai.instruction_key,
        )
        providers["infer"] = ai_provider
        ai_key = "infer"

    idle_key = "idle"
    providers[idle_key] = IdleCommandProvider()

    default_key = config.default_mode
    if default_key:
        default_key = teleop_aliases.get(default_key, default_key)
    if not default_key or default_key not in providers:
        default_key = ai_key or next(iter(providers.keys()))

    server = CommandService(
        providers=providers,
        default_key=default_key,
        host=config.network.host,
        port=config.network.port,
        api_token=config.network.api_token,
    )

    dispatcher = KeyboardModeDispatcher()
    manager = ModeManager(
        service=server,
        dispatcher=dispatcher,
        provider_aliases=teleop_aliases,
        ai_key=ai_key,
        idle_key=idle_key,
    )

    manager.start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop()


if __name__ == "__main__":
    main()
