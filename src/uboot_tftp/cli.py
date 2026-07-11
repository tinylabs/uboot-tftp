"""Command-line entry point for the config-driven daemon."""

from __future__ import annotations

import argparse
import os
import logging
import signal
from collections.abc import Callable
from pathlib import Path

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
    parser.add_argument(
        "--log-dir",
        help="Directory for per-session request/script logs.",
    )
    return parser


def build_server(
    config: DaemonConfig,
    *,
    log_dir: str | Path | None = None,
) -> DynamicContentServer:
    provider, uploads = build_runtime(config, log_dir=log_dir)
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


def build_runtime(
    config: DaemonConfig,
    *,
    log_dir: str | Path | None = None,
) -> tuple[ScriptedSessionProvider, InMemoryUploadStore]:
    sessions = InMemorySessionStore()
    uploads = InMemoryUploadStore(sessions)
    provider = ScriptedSessionProvider(
        config,
        sessions=sessions,
        upload_store=uploads,
        session_log_dir=log_dir,
    )
    return provider, uploads


def configure_logging(log_level: str) -> int:
    level_name = str(log_level).upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level)
    # Keep tftpy's packet/block chatter out of INFO unless the operator explicitly asks for DEBUG.
    logging.getLogger("tftpy").setLevel(logging.DEBUG if level <= logging.DEBUG else logging.WARNING)
    return level


def pidfile_path(config: DaemonConfig) -> Path:
    value = config.server.get("pidfile")
    if value:
        return Path(str(value)).resolve()
    return config.path.with_suffix(".pid")


def write_pidfile(path: str | Path, pid: int | None = None) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{os.getpid() if pid is None else int(pid)}\n")
    return target


def remove_pidfile(path: str | Path) -> None:
    target = Path(path).resolve()
    try:
        target.unlink()
    except FileNotFoundError:
        return


def find_config_pids(
    config_path: str | Path,
    *,
    proc_root: str | Path = "/proc",
    current_pid: int | None = None,
) -> list[int]:
    resolved = str(Path(config_path).resolve())
    root = Path(proc_root)
    if not root.exists():
        return []
    active_pid = os.getpid() if current_pid is None else int(current_pid)

    matches: list[int] = []
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == active_pid:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().split(b"\x00")
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        args = [part.decode("utf-8", errors="ignore") for part in cmdline if part]
        if not args:
            continue
        if not _is_daemon_argv(args):
            continue
        candidate = _config_arg_from_argv(args)
        if candidate is None:
            continue
        try:
            if str(Path(candidate).resolve()) == resolved:
                matches.append(pid)
        except OSError:
            continue
    return sorted(set(matches))


def resolve_reload_pid(
    config: DaemonConfig,
    *,
    explicit_pid: int | None = None,
    proc_root: str | Path = "/proc",
) -> int:
    if explicit_pid is not None:
        return int(explicit_pid)

    candidates = set(find_config_pids(config.path, proc_root=proc_root))
    pidfile = pidfile_path(config)
    try:
        pidfile_pid = int(pidfile.read_text().strip(), 0)
    except FileNotFoundError:
        pidfile_pid = None
    except ValueError as error:
        raise ValueError(f"invalid pidfile contents: {pidfile}") from error
    if pidfile_pid is not None:
        candidates.add(pidfile_pid)

    if not candidates:
        raise ValueError(f"no running instance found for config {config.path}")
    if len(candidates) > 1:
        raise ValueError(
            f"multiple running instances found for config {config.path}: {sorted(candidates)}"
        )
    return next(iter(candidates))


def reload_server(
    server: DynamicContentServer,
    *,
    config_path: str,
    rootdir: str | None = None,
    log_dir: str | Path | None = None,
) -> None:
    config = load_daemon_config(config_path, rootdir=rootdir)
    configure_logging(str(config.server.get("log_level", "INFO")))
    provider, uploads = build_runtime(config, log_dir=log_dir)
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
    log_dir: str | Path | None = None,
    signal_module=signal,
) -> Callable[[int, object], None] | None:
    sighup = getattr(signal_module, "SIGHUP", None)
    if sighup is None:
        LOGGER.info("SIGHUP is unavailable on this platform; config reload disabled")
        return None

    def handle_reload(signum, frame):  # noqa: ARG001
        LOGGER.info("Received signal %s, reloading configuration", signum)
        try:
            reload_server(
                server,
                config_path=config_path,
                rootdir=rootdir,
                log_dir=log_dir,
            )
        except Exception:
            LOGGER.exception("Failed to reload configuration from %s", config_path)
        else:
            LOGGER.info("Reloaded configuration from %s", config_path)

    signal_module.signal(sighup, handle_reload)
    return handle_reload


def _config_arg_from_argv(args: list[str]) -> str | None:
    for index, arg in enumerate(args):
        if arg == "--config" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--config="):
            return arg.partition("=")[2]
    return None


def _is_daemon_argv(args: list[str]) -> bool:
    joined = " ".join(args)
    if "uboot_tftp.cli" in joined:
        return True
    return any(
        Path(arg).name == "uboot-tftp"
        for arg in args
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_daemon_config(args.config, rootdir=args.rootdir)
    configure_logging(str(config.server.get("log_level", "INFO")))
    pid_path = write_pidfile(pidfile_path(config))
    LOGGER.info("Wrote pidfile %s", pid_path)

    server = build_server(config, log_dir=args.log_dir)
    install_reload_handler(
        server,
        config_path=args.config,
        rootdir=args.rootdir,
        log_dir=args.log_dir,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        server.close()
    finally:
        remove_pidfile(pid_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
