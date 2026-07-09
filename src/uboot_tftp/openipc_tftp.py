"""Wrapper entry point for the packaged OpenIPC example server."""

from __future__ import annotations

import sys
from importlib.resources import as_file, files

from .cli import main as cli_main


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    config_resource = files("uboot_tftp").joinpath("openipc.toml")
    with as_file(config_resource) as config_path:
        return cli_main(["--config", str(config_path), *args])


if __name__ == "__main__":
    raise SystemExit(main())
