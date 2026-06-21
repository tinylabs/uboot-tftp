"""Command-line entry point for the config-driven daemon."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import DaemonConfig, load_daemon_config
from .server import DynamicContentServer
from .scripted import ScriptedConfigProvider
from .uploads import DiskUploadStore, InMemoryUploadStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the openipc-tftp daemon.")
    parser.add_argument("config", help="Path to the daemon TOML configuration file.")
    return parser


def build_server(config: DaemonConfig) -> DynamicContentServer:
    server_config = config.server
    upload_dir = _resolve_config_path(
        config,
        server_config.get("upload_dir", server_config.get("upload")),
    )
    uploads = DiskUploadStore(upload_dir) if upload_dir else InMemoryUploadStore()
    provider = ScriptedConfigProvider(config, upload_store=uploads)

    return DynamicContentServer(
        address=str(server_config.get("address", "::")),
        port=int(server_config.get("port", 6969)),
        retries=int(server_config.get("retries", 3)),
        timeout=int(server_config.get("timeout", 5)),
        provider=provider,
        upload_store=uploads,
    )


def _resolve_config_path(config: DaemonConfig, value: object) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path
    return config.path.parent / path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_daemon_config(args.config)
    log_level = str(config.server.get("log_level", "INFO")).upper()
    logging.basicConfig(level=getattr(logging, log_level))

    server = build_server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
