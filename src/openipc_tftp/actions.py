"""Helpers for queuing U-Boot actions."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Iterable
from threading import RLock

from .sessions import InMemorySessionStore, UBootAction

UBOOT_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_#.-]*$")
PROBE_VARS = (
    "ipaddr",
    "serverip",
    "gatewayip",
    "netmask",
    "ethaddr",
    "serial#",
    "bootcmd",
    "bootargs",
    "bootdelay",
    "loadaddr",
    "baseaddr",
    "kernel_addr_r",
    "fdt_addr_r",
)


def validate_uboot_var_name(name: str) -> str:
    if not UBOOT_VAR_RE.match(name):
        raise ValueError(f"invalid U-Boot variable name: {name!r}")
    return name


def validate_uboot_command(command: str) -> str:
    if not command or "\n" in command or "\r" in command:
        raise ValueError(f"invalid U-Boot command: {command!r}")
    return command


def validate_tftp_path(path: str) -> str:
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise ValueError(f"invalid TFTP path: {path!r}")
    if "\n" in path or "\r" in path or '"' in path:
        raise ValueError(f"invalid TFTP path: {path!r}")
    return path


class UBootActionQueue:
    """Queue actions globally or for a specific client ethaddr."""

    def __init__(self, sessions: InMemorySessionStore) -> None:
        self._sessions = sessions
        self._global_actions: deque[UBootAction] = deque()
        self._targeted_actions: dict[str, deque[UBootAction]] = defaultdict(deque)
        self._lock = RLock()

    def get_uboot_var(self, name: str, ethaddr: str | None = None) -> UBootAction:
        action = UBootAction(kind="get_var", name=validate_uboot_var_name(name))
        self.queue(action, ethaddr=ethaddr)
        return action

    def set_uboot_var(
        self,
        name: str,
        value: str,
        *,
        saveenv: bool = False,
        ethaddr: str | None = None,
    ) -> UBootAction:
        action = UBootAction(
            kind="set_var",
            name=validate_uboot_var_name(name),
            value=value,
            saveenv=saveenv,
        )
        self.queue(action, ethaddr=ethaddr)
        return action

    def run_uboot_var(self, name: str, ethaddr: str | None = None) -> UBootAction:
        action = UBootAction(kind="run_var", name=validate_uboot_var_name(name))
        self.queue(action, ethaddr=ethaddr)
        return action

    def run_uboot_commands(
        self,
        commands: Iterable[str],
        *,
        name: str = "inline",
        ethaddr: str | None = None,
    ) -> UBootAction:
        action = UBootAction(
            kind="run_commands",
            name=validate_uboot_var_name(name),
            commands=tuple(validate_uboot_command(command) for command in commands),
        )
        if not action.commands:
            raise ValueError("run_uboot_commands requires at least one command")
        self.queue(action, ethaddr=ethaddr)
        return action

    def printenv(
        self,
        names: Iterable[str] = (),
        *,
        ethaddr: str | None = None,
    ) -> UBootAction:
        action = UBootAction(
            kind="printenv",
            name="printenv",
            commands=tuple(validate_uboot_var_name(name) for name in names),
        )
        self.queue(action, ethaddr=ethaddr)
        return action

    def reset(self, ethaddr: str | None = None) -> UBootAction:
        action = UBootAction(kind="reset", name="reset")
        self.queue(action, ethaddr=ethaddr)
        return action

    def boot(self, command: str = "boot", ethaddr: str | None = None) -> UBootAction:
        action = UBootAction(
            kind="boot",
            name="boot",
            value=validate_uboot_command(command),
        )
        self.queue(action, ethaddr=ethaddr)
        return action

    def sleep(self, seconds: int, ethaddr: str | None = None) -> UBootAction:
        if seconds < 0:
            raise ValueError("sleep seconds must be non-negative")
        action = UBootAction(kind="sleep", name="sleep", value=str(seconds))
        self.queue(action, ethaddr=ethaddr)
        return action

    def report(
        self,
        name: str,
        expression: str,
        *,
        ethaddr: str | None = None,
    ) -> UBootAction:
        action = UBootAction(
            kind="report",
            name=validate_uboot_var_name(name),
            value=expression,
        )
        self.queue(action, ethaddr=ethaddr)
        return action

    def probe(self, ethaddr: str | None = None) -> list[UBootAction]:
        actions = [self.get_uboot_var(name, ethaddr=ethaddr) for name in PROBE_VARS]
        return actions

    def export_env(
        self,
        *,
        path: str = "upload/env.txt",
        address: str = "${loadaddr}",
        ethaddr: str | None = None,
    ) -> UBootAction:
        action = UBootAction(
            kind="export_env",
            name=validate_tftp_path(path),
            value=validate_uboot_command(address),
        )
        self.queue(action, ethaddr=ethaddr)
        return action

    def queue(self, action: UBootAction, ethaddr: str | None = None) -> None:
        with self._lock:
            if ethaddr is None:
                self._global_actions.append(action)
            else:
                self._targeted_actions[ethaddr.lower()].append(action)

    def next_action(self, ethaddr: str) -> UBootAction | None:
        ethaddr = ethaddr.lower()
        with self._lock:
            targeted = self._targeted_actions[ethaddr]
            if targeted:
                return targeted.popleft()
            if self._global_actions:
                return self._global_actions.popleft()
        return self._sessions.next_action(ethaddr)

    def load_global_actions(self, actions: Iterable[UBootAction]) -> None:
        with self._lock:
            self._global_actions.extend(actions)
