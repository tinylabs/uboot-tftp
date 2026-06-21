"""System-tftp based client for exercising multi-round-trip daemon flows."""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .mkimage import extract_script_payload

TFTP_REMOTE_RE = re.compile(r'"[^"]*:(id=[^"]*)"')
UBOOT_VAR_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass(frozen=True)
class ClientConfig:
    host: str
    port: int
    client_id: str
    path: str
    rounds: int
    env: dict[str, str]
    target_env: dict[str, str]
    keep_dir: Path | None
    tftp_binary: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Exercise openipc-tftp RRQ/WRQ loops with the system tftp client."
    )
    parser.add_argument("host", help="TFTP server host.")
    parser.add_argument("--port", default=6969, type=int, help="TFTP server port.")
    parser.add_argument("--id", required=True, dest="client_id", help="Client id= value.")
    parser.add_argument(
        "--path",
        default="/",
        help="Initial path after id=<id>. Example: /bootstrap or /boot.",
    )
    parser.add_argument(
        "--rounds",
        default=5,
        type=int,
        help="Maximum RRQ rounds to follow.",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Variable used for script path substitution. Repeatable.",
    )
    parser.add_argument(
        "--target-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Mock target U-Boot env uploaded when the server requests get_env().",
    )
    parser.add_argument(
        "--target-env-file",
        type=Path,
        help="File containing KEY=VALUE lines to upload for get_env().",
    )
    parser.add_argument(
        "--keep-dir",
        type=Path,
        help="Keep downloaded scripts and generated upload files in this directory.",
    )
    parser.add_argument(
        "--tftp-binary",
        default="tftp",
        help="System tftp executable to call.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = ClientConfig(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        path=_normalize_path(args.path),
        rounds=args.rounds,
        env=_client_env(args.client_id, _parse_pairs(args.env)),
        target_env=_target_env(args.target_env, args.target_env_file),
        keep_dir=args.keep_dir,
        tftp_binary=args.tftp_binary,
    )
    run_client(config)
    return 0


def run_client(config: ClientConfig) -> None:
    if config.keep_dir is not None:
        config.keep_dir.mkdir(parents=True, exist_ok=True)
        workdir = _StaticWorkdir(config.keep_dir)
    else:
        workdir = tempfile.TemporaryDirectory(prefix="openipc-tftp-client-")

    with workdir as directory_name:
        directory = Path(directory_name)
        remote = _remote_filename(config.client_id, config.path)
        for round_number in range(1, config.rounds + 1):
            download_path = directory / f"round-{round_number}.scr.uimg"
            print(f"RRQ {round_number}: {remote}")
            _tftp_get(config, remote, download_path)
            script = extract_script_payload(download_path.read_bytes()).decode(
                "utf-8",
                errors="replace",
            )
            print(_indent(script.rstrip()))

            upload_remote = _find_upload_remote(script, config.env)
            if upload_remote is not None:
                upload_path = directory / f"round-{round_number}-env.txt"
                upload_path.write_text(_env_text(config.target_env))
                print(f"WRQ {round_number}: {upload_remote}")
                _tftp_put(config, upload_path, upload_remote)

            next_remote = _find_continue_remote(script, config.env)
            if next_remote is None:
                print("No follow-up RRQ found; stopping.")
                return
            remote = next_remote
        print(f"Stopped after {config.rounds} rounds.")


class _StaticWorkdir:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> str:
        return str(self.path)

    def __exit__(self, *args: object) -> None:
        return None


def _tftp_get(config: ClientConfig, remote: str, local: Path) -> None:
    _run_tftp(config, "get", remote, local)


def _tftp_put(config: ClientConfig, local: Path, remote: str) -> None:
    _run_tftp(config, "put", remote, local)


def _run_tftp(config: ClientConfig, operation: str, remote: str, local: Path) -> None:
    command = [
        config.tftp_binary,
        config.host,
        str(config.port),
        "-m",
        "binary",
        "-c",
        operation,
    ]
    if operation == "get":
        command.extend((remote, str(local)))
    else:
        command.extend((str(local), remote))
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as error:
        raise SystemExit(f"tftp binary not found: {config.tftp_binary}") from error
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"tftp {operation} failed with exit code {error.returncode}") from error


def _find_upload_remote(script: str, env: dict[str, str]) -> str | None:
    for remote in _remote_paths(script, env):
        if "/upload/" in remote:
            return remote
    return None


def _find_continue_remote(script: str, env: dict[str, str]) -> str | None:
    for remote in _remote_paths(script, env):
        if "/upload/" not in remote:
            return remote
    return None


def _remote_paths(script: str, env: dict[str, str]) -> list[str]:
    paths = []
    for match in TFTP_REMOTE_RE.finditer(script):
        paths.append(_substitute_uboot_vars(match.group(1), env))
    return paths


def _substitute_uboot_vars(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise SystemExit(f"script referenced ${{{name}}}; pass --env {name}=VALUE")
        return env[name]

    return UBOOT_VAR_RE.sub(replace, value)


def _remote_filename(client_id: str, path: str) -> str:
    return f"id={client_id}{path}"


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    return path if path.startswith("/") else f"/{path}"


def _client_env(client_id: str, values: dict[str, str]) -> dict[str, str]:
    return dict(values)


def _target_env(values: list[str], path: Path | None) -> dict[str, str]:
    env = {
        "bootcmd": "boot",
        "ethaddr": "02:11:22:33:44:55",
        "hostname": "testcam",
        "ipaddr": "192.168.1.50",
        "serverip": "192.168.1.10",
    }
    if path is not None:
        env.update(_parse_env_file(path))
    env.update(_parse_pairs(values))
    return env


def _parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def _parse_pairs(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        key, separator, item_value = value.partition("=")
        if not separator or not key:
            raise SystemExit(f"expected KEY=VALUE, got {value!r}")
        parsed[key] = item_value
    return parsed


def _env_text(env: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in sorted(env.items()))


def _indent(value: str) -> str:
    if not value:
        return "  <empty script>"
    return "\n".join(f"  {line}" for line in value.splitlines())


if __name__ == "__main__":
    raise SystemExit(main())
