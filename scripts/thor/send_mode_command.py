from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path


def build_payload(args: argparse.Namespace) -> dict:
    match args.command:
        case "teleop":
            if not args.value:
                raise SystemExit("teleop requires an alias argument")
            return {"teleop": args.value}
        case "infer":
            if not args.value:
                raise SystemExit("infer requires an instruction argument")
            return {"infer": args.value}
        case "idle":
            return {"idle": args.value or ""}
        case "shutdown":
            return {"shutdown": args.value or ""}
        case "data":
            payload: dict[str, object]
            if args.mode or args.value:
                payload = {"data": {}}
                if args.mode:
                    payload["data"]["mode"] = args.mode
                if args.value:
                    payload["data"]["command"] = args.value
                if not payload["data"]:
                    payload["data"] = ""
                return payload
            return {"data": ""}
        case "raw":
            if not args.value:
                raise SystemExit("raw command requires a JSON payload")
            try:
                parsed = json.loads(args.value)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSON for raw command: {exc}")
            if not isinstance(parsed, dict):
                raise SystemExit("raw command must be a JSON object")
            return parsed
        case _:
            raise SystemExit(f"unsupported command: {args.command}")


def send(socket_path: Path, payload: dict, timeout: float | None = 2.0) -> str:
    message = json.dumps(payload) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        if timeout is not None:
            client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(message.encode("utf-8"))
        response = client.recv(1024)
    return response.decode("utf-8", errors="replace").strip()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Send a Brainbot mode command over the socket dispatcher")
    parser.add_argument(
        "--socket",
        type=Path,
        default=Path("/tmp/brainbot_modesock"),
        help="Path to the UNIX socket exposed by the command service",
    )
    parser.add_argument("command", choices=("teleop", "infer", "idle", "shutdown", "data", "raw"))
    parser.add_argument("value", nargs="?", help="Command argument (alias, instruction, reason, etc.)")
    parser.add_argument("--mode", help="Target mode when sending a data command")
    parser.add_argument("--timeout", type=float, default=2.0, help="Socket timeout in seconds (default: 2.0)")

    args = parser.parse_args(argv)

    payload = build_payload(args)
    response = send(args.socket, payload, timeout=args.timeout)
    print(response or "(no response)")


if __name__ == "__main__":
    main()
