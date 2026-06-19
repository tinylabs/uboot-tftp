"""Command-line entry point for the scaffold server."""

from __future__ import annotations

import argparse
import logging

from .server import DynamicContentServer
from .uboot import UBootScriptProvider, UBootScriptRenderer
from .uploads import DiskUploadStore, InMemoryUploadStore


def parse_set_var(value: str) -> tuple[str, str]:
    name, separator, var_value = value.partition("=")
    if not separator or not name:
        raise argparse.ArgumentTypeError("--set-var must use NAME=VALUE")
    return name, var_value


def parse_report(value: str) -> tuple[str, str]:
    name, separator, expression = value.partition("=")
    if not separator or not name:
        raise argparse.ArgumentTypeError("--report must use NAME=EXPRESSION")
    return name, expression


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the openipc-tftp server.")
    parser.add_argument("--address", default="::", help="Address to bind.")
    parser.add_argument("--port", default=6969, type=int, help="UDP port to bind.")
    parser.add_argument("--retries", default=3, type=int, help="TFTP retry count.")
    parser.add_argument("--timeout", default=5, type=int, help="TFTP timeout seconds.")
    parser.add_argument(
        "--no-loop",
        action="store_true",
        help="Return a script that does not request the next script.",
    )
    parser.add_argument(
        "--ethaddr",
        help="Only send queued helper actions to this client MAC address.",
    )
    parser.add_argument(
        "--get-var",
        action="append",
        default=[],
        metavar="NAME",
        help="Queue a U-Boot variable read. Can be used more than once.",
    )
    parser.add_argument(
        "--set-var",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        type=parse_set_var,
        help="Queue a U-Boot variable write. Can be used more than once.",
    )
    parser.add_argument(
        "--saveenv",
        action="store_true",
        help="Persist all --set-var changes with saveenv.",
    )
    parser.add_argument(
        "--run-var",
        action="append",
        default=[],
        metavar="NAME",
        help="Queue `run NAME`. Can be used more than once.",
    )
    parser.add_argument(
        "--run-cmd",
        action="append",
        default=[],
        metavar="COMMAND",
        help="Queue one inline U-Boot command. Repeated values run as one batch.",
    )
    parser.add_argument(
        "--run-name",
        default="inline",
        help="Name used in the callback for a --run-cmd batch.",
    )
    parser.add_argument(
        "--printenv",
        action="store_true",
        help="Queue printenv for serial-console debugging.",
    )
    parser.add_argument(
        "--printenv-var",
        action="append",
        default=[],
        metavar="NAME",
        help="Queue printenv NAME. Can be used more than once.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Queue reads for common U-Boot environment variables.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Queue a reset command.",
    )
    parser.add_argument(
        "--boot",
        nargs="?",
        const="boot",
        metavar="COMMAND",
        help="Queue a boot command. Defaults to `boot` when no command is given.",
    )
    parser.add_argument(
        "--sleep",
        action="append",
        default=[],
        metavar="SECONDS",
        type=int,
        help="Queue sleep SECONDS. Can be used more than once.",
    )
    parser.add_argument(
        "--report",
        action="append",
        default=[],
        metavar="NAME=EXPRESSION",
        type=parse_report,
        help="Queue a generic RRQ report callback.",
    )
    parser.add_argument(
        "--upload-dir",
        help="Persist tftpput uploads under this directory.",
    )
    parser.add_argument(
        "--export-env",
        nargs="?",
        const="upload/env.txt",
        metavar="PATH",
        help="Export the full U-Boot environment and tftpput it to PATH.",
    )
    parser.add_argument(
        "--export-env-addr",
        default="${loadaddr}",
        metavar="ADDRESS",
        help="Memory address used for --export-env. Defaults to ${loadaddr}.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Python logging level.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level))

    provider = UBootScriptProvider(
        renderer=UBootScriptRenderer(continue_loop=not args.no_loop)
    )
    uploads = DiskUploadStore(args.upload_dir) if args.upload_dir else InMemoryUploadStore()
    for name in args.get_var:
        provider.get_uboot_var(name, ethaddr=args.ethaddr)
        logging.info("Queued get_uboot_var(%r) ethaddr=%s", name, args.ethaddr or "*")
    for name, value in args.set_var:
        provider.set_uboot_var(
            name,
            value,
            saveenv=args.saveenv,
            ethaddr=args.ethaddr,
        )
        logging.info(
            "Queued set_uboot_var(%r, %r, saveenv=%s) ethaddr=%s",
            name,
            value,
            args.saveenv,
            args.ethaddr or "*",
        )
    for name in args.run_var:
        provider.run_uboot_var(name, ethaddr=args.ethaddr)
        logging.info("Queued run_uboot_var(%r) ethaddr=%s", name, args.ethaddr or "*")
    if args.run_cmd:
        provider.run_uboot_commands(
            args.run_cmd,
            name=args.run_name,
            ethaddr=args.ethaddr,
        )
        logging.info(
            "Queued run_uboot_commands(%r, name=%r) ethaddr=%s",
            args.run_cmd,
            args.run_name,
            args.ethaddr or "*",
        )
    if args.printenv or args.printenv_var:
        provider.printenv(args.printenv_var, ethaddr=args.ethaddr)
        names = ", ".join(args.printenv_var) if args.printenv_var else "*"
        logging.info("Queued printenv(%s) ethaddr=%s", names, args.ethaddr or "*")
    if args.probe:
        provider.probe(ethaddr=args.ethaddr)
        logging.info("Queued probe() ethaddr=%s", args.ethaddr or "*")
    for seconds in args.sleep:
        provider.sleep(seconds, ethaddr=args.ethaddr)
        logging.info("Queued sleep(%s) ethaddr=%s", seconds, args.ethaddr or "*")
    for name, expression in args.report:
        provider.report(name, expression, ethaddr=args.ethaddr)
        logging.info(
            "Queued report(%r, %r) ethaddr=%s",
            name,
            expression,
            args.ethaddr or "*",
        )
    if args.boot is not None:
        provider.boot(args.boot, ethaddr=args.ethaddr)
        logging.info("Queued boot(%r) ethaddr=%s", args.boot, args.ethaddr or "*")
    if args.reset:
        provider.reset(ethaddr=args.ethaddr)
        logging.info("Queued reset() ethaddr=%s", args.ethaddr or "*")
    if args.export_env:
        provider.export_env(
            path=args.export_env,
            address=args.export_env_addr,
            ethaddr=args.ethaddr,
        )
        logging.info(
            "Queued export_env(path=%r, address=%r) ethaddr=%s",
            args.export_env,
            args.export_env_addr,
            args.ethaddr or "*",
        )

    server = DynamicContentServer(
        address=args.address,
        port=args.port,
        retries=args.retries,
        timeout=args.timeout,
        provider=provider,
        upload_store=uploads,
    )

    try:
        server.run()
    except KeyboardInterrupt:
        server.close()
        for upload in uploads.all():
            logging.info(
                "Captured upload filename=%s size=%d peer=%s:%s",
                upload.filename,
                upload.size,
                upload.peer[0],
                upload.peer[1],
            )
        if args.upload_dir:
            logging.info("Uploads persisted under %s", args.upload_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
