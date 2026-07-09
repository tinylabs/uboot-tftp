"""Command-line entry point for the config-driven daemon."""

from __future__ import annotations

import argparse
import logging
import signal
from collections.abc import Callable

from .config import DaemonConfig, load_daemon_config
from .scripted import ScriptedSessionProvider
from .server import DynamicContentServer
from .sessions import InMemorySessionStore
from .uploads import InMemoryUploadStore

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the uboot-tftp daemon.")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to the daemon TOML configuration file.",
    )
    parser.add_argument(
        "--rootdir",
        help="Absolute path to override [server].rootdir from the config file.",
    )
    return parser


def build_server(config: DaemonConfig) -> DynamicContentServer:
    provider, uploads = build_runtime(config)
    server_config = config.server
    return DynamicContentServer(
        address=str(server_config.get("address", "::")),
        port=int(server_config.get("port", 6969)),
        retries=int(server_config.get("retries", 3)),
        timeout=int(server_config.get("timeout", 5)),
        provider=provider,
        upload_store=uploads,
        tftproot=config.static_root,
    )


def build_runtime(config: DaemonConfig) -> tuple[ScriptedSessionProvider, InMemoryUploadStore]:
    sessions = InMemorySessionStore()
    uploads = InMemoryUploadStore(sessions)
    provider = ScriptedSessionProvider(config, sessions=sessions, upload_store=uploads)
    return provider, uploads


def configure_logging(log_level: str) -> int:
    level_name = str(log_level).upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level)
    # Keep tftpy's packet/block chatter out of INFO unless the operator explicitly asks for DEBUG.
    logging.getLogger("tftpy").setLevel(logging.DEBUG if level <= logging.DEBUG else logging.WARNING)
    return level


def reload_server(
    server: DynamicContentServer,
    *,
    config_path: str,
    rootdir: str | None = None,
) -> None:
    config = load_daemon_config(config_path, rootdir=rootdir)
    configure_logging(str(config.server.get("log_level", "INFO")))
    provider, uploads = build_runtime(config)
    server.reload(
        provider=provider,
        upload_store=uploads,
        tftproot=config.static_root,
        address=str(config.server.get("address", "::")),
        port=int(config.server.get("port", 6969)),
        retries=int(config.server.get("retries", 3)),
        timeout=int(config.server.get("timeout", 5)),
    )


def install_reload_handler(
    server: DynamicContentServer,
    *,
    config_path: str,
    rootdir: str | None = None,
    signal_module=signal,
) -> Callable[[int, object], None] | None:
    sighup = getattr(signal_module, "SIGHUP", None)
    if sighup is None:
        LOGGER.info("SIGHUP is unavailable on this platform; config reload disabled")
        return None

    def handle_reload(signum, frame):  # noqa: ARG001
        LOGGER.info("Received signal %s, reloading configuration", signum)
        try:
            reload_server(server, config_path=config_path, rootdir=rootdir)
        except Exception:
            LOGGER.exception("Failed to reload configuration from %s", config_path)
        else:
            LOGGER.info("Reloaded configuration from %s", config_path)

    signal_module.signal(sighup, handle_reload)
    return handle_reload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_daemon_config(args.config, rootdir=args.rootdir)
    configure_logging(str(config.server.get("log_level", "INFO")))

    server = build_server(config)
    install_reload_handler(server, config_path=args.config, rootdir=args.rootdir)
    try:
        server.run()
    except KeyboardInterrupt:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
