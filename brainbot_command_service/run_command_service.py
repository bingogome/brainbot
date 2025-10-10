from __future__ import annotations

import argparse
from pathlib import Path

from lerobot.processor import make_default_processors
from lerobot.teleoperators.utils import make_teleoperator_from_config

from gr00t.eval.service import ExternalRobotInferenceClient
from brainbot_core.config import ServerRuntimeConfig, load_server_config
from brainbot_mode_dispatcher import CLIModeDispatcher

from brainbot_webviz import VisualizationServer

from . import (
    AICommandProvider,
    CommandProvider,
    CommandService,
    IdleCommandProvider,
    LocalTeleopCommandProvider,
    ModeManager,
    RemoteTeleopCommandProvider,
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)

    config: ServerRuntimeConfig = load_server_config(args.config)
    providers: dict[str, CommandProvider] = {}
    teleop_aliases: dict[str, str] = {}
    for name, endpoint in config.teleops.items():
        key = f"teleop:{name}"
        if endpoint.mode == "remote" and endpoint.remote is not None:
            providers[key] = RemoteTeleopCommandProvider(
                host=endpoint.remote.host,
                port=endpoint.remote.port,
                timeout_ms=endpoint.remote.timeout_ms,
                api_token=endpoint.remote.api_token,
            )
        elif endpoint.mode == "local" and endpoint.local is not None:
            teleop = make_teleoperator_from_config(endpoint.local)
            teleop_action_processor, robot_action_processor, _ = make_default_processors()
            providers[key] = LocalTeleopCommandProvider(
                teleop=teleop,
                teleop_action_processor=teleop_action_processor,
                robot_action_processor=robot_action_processor,
            )
        else:
            raise ValueError(f"Invalid teleop endpoint configuration for '{name}'")

        teleop_aliases[name] = key
        teleop_aliases[key] = key
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

    visualizer: VisualizationServer | None = None
    if config.webviz:
        visualizer = VisualizationServer(host=config.webviz.host, port=config.webviz.port)
        visualizer.start()

    server = CommandService(
        providers=providers,
        default_key=default_key,
        host=config.network.host,
        port=config.network.port,
        api_token=config.network.api_token,
        exchange_hook=(visualizer.update if visualizer else None),
    )

    dispatcher = CLIModeDispatcher()
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
        if visualizer:
            visualizer.stop()


if __name__ == "__main__":
    main()
