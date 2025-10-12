from __future__ import annotations

import argparse
import logging
import zmq
from pathlib import Path

try:
    from lerobot.processor import make_default_processors
except ImportError:  # compatibility with newer LeRobot releases
    from lerobot.processor.factory import make_default_processors  # type: ignore
from lerobot.teleoperators.utils import make_teleoperator_from_config

from gr00t.eval.service import ExternalRobotInferenceClient
from brainbot_core.config import AIClientConfig, ServerRuntimeConfig, WebVizConfig, load_server_config
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


logger = logging.getLogger(__name__)


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
    ai_cfg = config.ai or AIClientConfig()
    ai_client = ExternalRobotInferenceClient(
        host=ai_cfg.host,
        port=ai_cfg.port,
        timeout_ms=ai_cfg.timeout_ms,
        api_token=ai_cfg.api_token,
    )
    try:
        if ai_cfg.timeout_ms:
            ai_client.socket.setsockopt(zmq.RCVTIMEO, ai_cfg.timeout_ms)
            ai_client.socket.setsockopt(zmq.SNDTIMEO, ai_cfg.timeout_ms)
        if not ai_client.ping():
            logger.warning("GR00T inference server at %s:%s did not respond to ping", ai_cfg.host, ai_cfg.port)
    except zmq.error.Again:
        logger.warning("GR00T inference server ping timed out (host=%s port=%s)", ai_cfg.host, ai_cfg.port)
    except Exception as exc:
        logger.warning("Could not ping GR00T inference server (%s)", exc)
    ai_provider = AICommandProvider(
        client=ai_client,
        instruction_key=ai_cfg.instruction_key,
    )
    providers["infer"] = ai_provider
    ai_key: str | None = "infer"

    idle_key = "idle"
    providers[idle_key] = IdleCommandProvider()

    default_key = config.default_mode
    if default_key:
        default_key = teleop_aliases.get(default_key, default_key)
    if not default_key or default_key not in providers:
        default_key = ai_key or next(iter(providers.keys()))

    webviz_cfg = config.webviz or WebVizConfig()
    camera_host = config.camera_stream.host if config.camera_stream else None
    camera_port = config.camera_stream.port if config.camera_stream else None
    visualizer = VisualizationServer(
        host=webviz_cfg.host,
        port=webviz_cfg.port,
        camera_host=camera_host,
        camera_port=camera_port,
    )
    visualizer.start()

    server = CommandService(
        providers=providers,
        default_key=default_key,
        host=config.network.host,
        port=config.network.port,
        api_token=config.network.api_token,
        exchange_hook=visualizer.update,
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
        visualizer.stop()


if __name__ == "__main__":
    main()
