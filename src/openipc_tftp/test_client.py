"""Small helper for inspecting images and simulating U-Boot TFTP flows."""

from __future__ import annotations

import argparse
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tftpy import TftpClient

from .mkimage import extract_script_payload

UPLOAD_RE = re.compile(
    r"\b(?P<command>[A-Za-z0-9_]+)\s+\$\{[^}]+\}\s+(?P<size>0x[0-9A-Fa-f]+|\d+)\s+"
    r'"(?P<server>[^":]+):(?P<remote>id=[^"]+)"'
)
DOWNLOAD_RE = re.compile(
    r"\b(?P<command>[A-Za-z0-9_]+)\s+\$\{[^}]+\}\s+"
    r'"(?P<server>[^":]+):(?P<remote>id=[^"]+)"'
)
UBOOT_VAR_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass(frozen=True)
class UploadAction:
    command: str
    server: str
    remote: str
    size: int


@dataclass(frozen=True)
class DownloadAction:
    command: str
    server: str
    remote: str


@dataclass(frozen=True)
class FlowActions:
    uploads: tuple[UploadAction, ...]
    downloads: tuple[DownloadAction, ...]


@dataclass(frozen=True)
class ClientConfig:
    host: str
    port: int
    client_id: str
    path: str
    rounds: int
    timeout: int
    retries: int
    keep_dir: Path | None
    dummy_byte: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract a U-Boot script image or simulate a U-Boot TFTP flow."
    )
    parser.add_argument(
        "target",
        help="Image path to extract or TFTP server host to simulate against.",
    )
    parser.add_argument("--id", dest="client_id", help="Session id=<ident> value.")
    parser.add_argument(
        "--path",
        default="/bootstrap",
        help="Initial session path after id=<ident>.",
    )
    parser.add_argument("--port", default=6969, type=int, help="TFTP server port.")
    parser.add_argument(
        "--rounds",
        default=10,
        type=int,
        help="Maximum RRQ rounds before stopping.",
    )
    parser.add_argument("--timeout", default=5, type=int, help="TFTP timeout.")
    parser.add_argument("--retries", default=3, type=int, help="TFTP retries.")
    parser.add_argument(
        "--keep-dir",
        type=Path,
        help="Directory to keep downloaded script images.",
    )
    parser.add_argument(
        "--dummy-byte",
        default="X",
        help="Single byte used to fill dummy WRQ payloads.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.client_id is None:
        return extract_main(Path(args.target))
    config = ClientConfig(
        host=args.target,
        port=args.port,
        client_id=args.client_id,
        path=_normalize_path(args.path),
        rounds=args.rounds,
        timeout=args.timeout,
        retries=args.retries,
        keep_dir=args.keep_dir,
        dummy_byte=_parse_dummy_byte(args.dummy_byte),
    )
    run_client(config)
    return 0


def extract_main(image: Path) -> int:
    print(extract_script(image).decode("utf-8", errors="replace"))
    return 0


def extract_script(image: Path) -> bytes:
    return extract_script_payload(image.read_bytes())


def run_client(config: ClientConfig, client_factory=TftpClient) -> None:
    if config.keep_dir is not None:
        config.keep_dir.mkdir(parents=True, exist_ok=True)
        workdir = _StaticWorkdir(config.keep_dir)
    else:
        workdir = tempfile.TemporaryDirectory(prefix="openipc-tftp-client-")

    with workdir as directory_name:
        directory = Path(directory_name)
        env = _initial_env(config)
        remote = f"id={config.client_id}{config.path}"
        for round_number in range(1, config.rounds + 1):
            image_path = directory / f"round-{round_number}.uimg"
            print(f"RRQ {round_number}: {remote}")
            _download(client_factory, config, remote, image_path)
            script = extract_script(image_path).decode("utf-8", errors="replace")
            _print_script(script)
            _update_env_from_script(script, env, config)
            actions = parse_flow_actions(script)
            _echo_non_transfer_commands(script)

            if actions.uploads:
                for upload in actions.uploads:
                    remote_upload = _substitute_uboot_vars(upload.remote, env)
                    print(
                        f"WRQ {round_number}: {remote_upload} "
                        f"({upload.size} bytes via {upload.command})"
                    )
                    _upload_dummy(client_factory, config, upload, remote_upload)
                next_remote = choose_next_remote(actions, env, prefer_recv="ok")
            else:
                next_remote = choose_next_remote(actions, env)

            if next_remote is None:
                print("No continuation RRQ found; stopping.")
                return
            remote = next_remote

        print(f"Stopped after {config.rounds} rounds.")


def parse_flow_actions(script: str) -> FlowActions:
    uploads = tuple(
        UploadAction(
            command=match.group("command"),
            server=match.group("server"),
            remote=match.group("remote"),
            size=int(match.group("size"), 0),
        )
        for match in UPLOAD_RE.finditer(script)
    )
    upload_remotes = {upload.remote for upload in uploads}
    downloads = tuple(
        DownloadAction(
            command=match.group("command"),
            server=match.group("server"),
            remote=match.group("remote"),
        )
        for match in DOWNLOAD_RE.finditer(script)
        if match.group("remote") not in upload_remotes
    )
    return FlowActions(uploads=uploads, downloads=downloads)


def choose_next_remote(
    actions: FlowActions,
    env: dict[str, str],
    prefer_recv: str | None = None,
) -> str | None:
    if prefer_recv is not None:
        marker = f"/recv={prefer_recv}"
        for download in actions.downloads:
            if marker in download.remote:
                return _substitute_uboot_vars(download.remote, env)
    return _substitute_uboot_vars(actions.downloads[0].remote, env) if actions.downloads else None


def _download(client_factory, config: ClientConfig, remote: str, output: Path) -> None:
    client = client_factory(config.host, port=config.port)
    client.download(remote, str(output), timeout=config.timeout, retries=config.retries)


def _upload_dummy(
    client_factory,
    config: ClientConfig,
    upload: UploadAction,
    remote: str,
) -> None:
    payload = _build_dummy_env_export(config, upload.size)
    client = client_factory(config.host, port=config.port)
    with tempfile.NamedTemporaryFile("w+b") as fileobj:
        fileobj.write(payload)
        fileobj.flush()
        fileobj.seek(0)
        client.upload(
            remote,
            fileobj,
            timeout=config.timeout,
            retries=config.retries,
        )


def _print_script(script: str) -> None:
    print("Script:")
    for line in script.rstrip().splitlines():
        print(f"  {line}")
    if not script.strip():
        print("  <empty>")


def _echo_non_transfer_commands(script: str) -> None:
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "id=" in stripped and ('"' in stripped or "'" in stripped):
            continue
        print(f"CMD: {stripped}")


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    return path if path.startswith("/") else f"/{path}"


def _parse_dummy_byte(value: str) -> int:
    if len(value) != 1:
        raise SystemExit("--dummy-byte must be exactly one character")
    return ord(value)


def _build_dummy_env_export(config: ClientConfig, size: int) -> bytes:
    payload = _build_dummy_env_export_unpadded(config)
    if len(payload) >= size:
        return payload[:size]
    return payload + (b"\0" * (size - len(payload)))


def _initial_env(config: ClientConfig) -> dict[str, str]:
    return {
        "bootcmd": f"echo boot {config.client_id}",
        "ethaddr": f"02:00:00:00:00:{config.dummy_byte:02x}",
        "hostname": config.client_id,
        "ipaddr": "192.168.1.50",
        "serverip": config.host,
    }


def _update_env_from_script(script: str, env: dict[str, str], config: ClientConfig) -> None:
    if "env export -t " in script:
        env["filesize"] = format(len(_build_dummy_env_export_unpadded(config)), "x")


def _substitute_uboot_vars(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return env.get(name, match.group(0))

    return UBOOT_VAR_RE.sub(replace, value)


def _build_dummy_env_export_unpadded(config: ClientConfig) -> bytes:
    text = "\0".join(
        (
            f"bootcmd=echo boot {config.client_id}",
            f"ethaddr=02:00:00:00:00:{config.dummy_byte:02x}",
            f"hostname={config.client_id}",
            "ipaddr=192.168.1.50",
            f"serverip={config.host}",
        )
    ) + "\0"
    return text.encode("utf-8")


class _StaticWorkdir:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> str:
        return str(self.path)

    def __exit__(self, *args: object) -> None:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
