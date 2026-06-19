"""U-Boot script rendering and dynamic provider implementation."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .actions import UBootActionQueue
from .mkimage import LegacyScriptImageCompiler
from .protocol import ClientMessage, parse_client_filename
from .providers import ContentRequest, ContentResult, DynamicContentProvider
from .sessions import ClientSession, InMemorySessionStore, UBootAction

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class UBootScriptRenderer:
    """Render the next script sent to a U-Boot client."""

    baseaddr: str = "${baseaddr}"
    serverip: str = "${serverip}"
    next_path: str = (
        "ethaddr=${ethaddr}/env/"
        "ipaddr=${ipaddr}/serverip=${serverip}/serial=${serial#}"
    )
    commands: tuple[str, ...] = field(
        default_factory=lambda: (
            'echo "openipc-tftp connected: ${ethaddr}"',
            'echo "reporting environment"',
        )
    )
    continue_loop: bool = True

    def render(
        self,
        message: ClientMessage,
        session: ClientSession,
        action: UBootAction | None = None,
    ) -> str:
        lines = [
            f'echo "openipc-tftp session {session.sequence} for {message.ethaddr}"',
            *self.commands,
        ]
        if action is not None:
            lines.extend(self._render_action(action))
        if self.continue_loop:
            lines.append(self._render_continue(action))
        return "\n".join(lines) + "\n"

    def _render_continue(self, action: UBootAction | None) -> str:
        return (
            f'if tftpboot {self.baseaddr} "{self.serverip}:{self._next_path(action)}"; '
            f"then source {self.baseaddr}; "
            'else echo "openipc-tftp: stopping because tftpboot failed"; fi'
        )

    def _render_action(self, action: UBootAction) -> tuple[str, ...]:
        if action.kind == "get_var":
            return (
                f'echo "getting {action.name}"',
            )
        if action.kind == "set_var":
            value = _quote_uboot_value(action.value or "")
            lines = [
                f'echo "setting {action.name}"',
                f"setenv {action.name} {value}",
            ]
            if action.saveenv:
                lines.append("saveenv")
            return tuple(lines)
        if action.kind == "run_var":
            return (
                f'echo "running {action.name}"',
                f"run {action.name}",
            )
        if action.kind == "run_commands":
            return (
                f'echo "running {action.name}"',
                *action.commands,
            )
        if action.kind == "printenv":
            if action.commands:
                return tuple(f"printenv {name}" for name in action.commands)
            return ("printenv",)
        if action.kind == "reset":
            return (
                'echo "resetting"',
                "reset",
            )
        if action.kind == "boot":
            return (
                'echo "booting"',
                action.value or "boot",
            )
        if action.kind == "sleep":
            return (
                f'echo "sleeping {action.value or "0"}"',
                f"sleep {action.value or '0'}",
            )
        if action.kind == "report":
            return (
                f'echo "reporting {action.name}"',
            )
        if action.kind == "export_env":
            address = action.value or "${loadaddr}"
            return (
                'echo "exporting environment"',
                f"env export -t {address}",
                (
                    f'if tftpput {address} ${{filesize}} "{self.serverip}:'
                    f'ethaddr=${{ethaddr}}/{action.name}"; '
                    'then echo "environment uploaded"; '
                    'else echo "environment upload failed"; fi'
                ),
            )
        raise ValueError(f"unknown U-Boot action kind: {action.kind!r}")

    def _next_path(self, action: UBootAction | None) -> str:
        if action is None:
            return self.next_path
        if action.kind == "get_var":
            return f"ethaddr=${{ethaddr}}/var/{action.name}=${{{action.name}}}"
        if action.kind == "set_var":
            return f"ethaddr=${{ethaddr}}/set/{action.name}=ok"
        if action.kind == "run_var":
            return f"ethaddr=${{ethaddr}}/run/{action.name}=ok"
        if action.kind == "run_commands":
            return f"ethaddr=${{ethaddr}}/run/{action.name}=ok"
        if action.kind == "printenv":
            return "ethaddr=${ethaddr}/printenv/printenv=ok"
        if action.kind == "sleep":
            return f"ethaddr=${{ethaddr}}/sleep/{action.value or '0'}=ok"
        if action.kind == "report":
            return f"ethaddr=${{ethaddr}}/report/{action.name}={action.value or ''}"
        if action.kind == "boot":
            return "ethaddr=${ethaddr}/boot/boot=ok"
        if action.kind == "reset":
            return "ethaddr=${ethaddr}/reset/reset=ok"
        if action.kind == "export_env":
            return "ethaddr=${ethaddr}/export-env/export-env=ok"
        raise ValueError(f"unknown U-Boot action kind: {action.kind!r}")


class UBootScriptProvider(DynamicContentProvider):
    """Dynamic provider that speaks the RRQ filename protocol to U-Boot."""

    def __init__(
        self,
        *,
        sessions: InMemorySessionStore | None = None,
        renderer: UBootScriptRenderer | None = None,
        compiler: LegacyScriptImageCompiler | None = None,
        actions: UBootActionQueue | None = None,
    ) -> None:
        self.sessions = sessions or InMemorySessionStore()
        self.renderer = renderer or UBootScriptRenderer()
        self.compiler = compiler or LegacyScriptImageCompiler()
        self.actions = actions or UBootActionQueue(self.sessions)

    def get_uboot_var(
        self,
        name: str,
        ethaddr: str | None = None,
    ) -> UBootAction:
        return self.actions.get_uboot_var(name, ethaddr=ethaddr)

    def set_uboot_var(
        self,
        name: str,
        value: str,
        *,
        saveenv: bool = False,
        ethaddr: str | None = None,
    ) -> UBootAction:
        return self.actions.set_uboot_var(
            name,
            value,
            saveenv=saveenv,
            ethaddr=ethaddr,
        )

    def run_uboot_var(self, name: str, ethaddr: str | None = None) -> UBootAction:
        return self.actions.run_uboot_var(name, ethaddr=ethaddr)

    def run_uboot_commands(
        self,
        commands: tuple[str, ...] | list[str],
        *,
        name: str = "inline",
        ethaddr: str | None = None,
    ) -> UBootAction:
        return self.actions.run_uboot_commands(commands, name=name, ethaddr=ethaddr)

    def printenv(
        self,
        names: tuple[str, ...] | list[str] = (),
        ethaddr: str | None = None,
    ) -> UBootAction:
        return self.actions.printenv(names, ethaddr=ethaddr)

    def reset(self, ethaddr: str | None = None) -> UBootAction:
        return self.actions.reset(ethaddr=ethaddr)

    def boot(self, command: str = "boot", ethaddr: str | None = None) -> UBootAction:
        return self.actions.boot(command, ethaddr=ethaddr)

    def sleep(self, seconds: int, ethaddr: str | None = None) -> UBootAction:
        return self.actions.sleep(seconds, ethaddr=ethaddr)

    def report(
        self,
        name: str,
        expression: str,
        *,
        ethaddr: str | None = None,
    ) -> UBootAction:
        return self.actions.report(name, expression, ethaddr=ethaddr)

    def probe(self, ethaddr: str | None = None) -> list[UBootAction]:
        return self.actions.probe(ethaddr=ethaddr)

    def export_env(
        self,
        *,
        path: str = "upload/env.txt",
        address: str = "${loadaddr}",
        ethaddr: str | None = None,
    ) -> UBootAction:
        return self.actions.export_env(path=path, address=address, ethaddr=ethaddr)

    def fetch(self, request: ContentRequest) -> ContentResult:
        message = parse_client_filename(request.filename)
        session = self.sessions.record(message)
        action = self.actions.next_action(message.ethaddr)
        LOGGER.info(
            "U-Boot RRQ from ethaddr=%s channel=%s values=%s",
            message.ethaddr,
            message.channel,
            message.values,
        )
        if message.channel == "var":
            for name, value in message.values.items():
                LOGGER.info("U-Boot var ethaddr=%s %s=%s", message.ethaddr, name, value)
        elif message.channel == "set":
            for name, status in message.values.items():
                LOGGER.info(
                    "U-Boot set ethaddr=%s %s=%s",
                    message.ethaddr,
                    name,
                    status,
                )
        elif message.channel == "report":
            for name, value in message.values.items():
                LOGGER.info(
                    "U-Boot report ethaddr=%s %s=%s",
                    message.ethaddr,
                    name,
                    value,
                )
        elif message.channel in {
            "run",
            "boot",
            "reset",
            "sleep",
            "printenv",
            "probe",
            "export-env",
        }:
            for name, status in message.values.items():
                LOGGER.info(
                    "U-Boot action ethaddr=%s channel=%s %s=%s",
                    message.ethaddr,
                    message.channel,
                    name,
                    status,
                )
        if action is not None:
            LOGGER.info(
                "Sending U-Boot action ethaddr=%s kind=%s name=%s",
                message.ethaddr,
                action.kind,
                action.name,
            )
        script = self.renderer.render(message, session, action)
        return ContentResult.from_bytes(self.compiler.compile(script))


def _quote_uboot_value(value: str) -> str:
    if value == "":
        return ""
    if any(character.isspace() for character in value) or any(
        character in value for character in '"$;\\'
    ):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
        return f'"{escaped}"'
    return value
