"""External validation tool for daemon config and user script syntax."""

from __future__ import annotations

import argparse
import os
import signal
import sys

from .cli import resolve_reload_pid
from .config import check_user_script_syntax, load_daemon_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate config.toml and the configured user script."
    )
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
        "--reload",
        action="store_true",
        help="After validation succeeds, send SIGHUP to the running daemon.",
    )
    parser.add_argument(
        "--pid",
        type=int,
        help="Target daemon PID for --reload.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        config = load_daemon_config(args.config, rootdir=args.rootdir)
        script_path = check_user_script_syntax(config.script_path)
        print(f"Config OK: {config.path}")
        print(f"Script OK: {script_path}")
        if args.reload:
            sighup = getattr(signal, "SIGHUP", None)
            if sighup is None:
                raise RuntimeError("SIGHUP is unavailable on this platform")
            pid = resolve_reload_pid(config, explicit_pid=args.pid)
            os.kill(pid, sighup)
            print(f"Reload signal sent: pid={pid} signal=SIGHUP")
        return 0
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
