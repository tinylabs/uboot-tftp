"""Framework command registry and capability probe helpers."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Literal

CommandPolicy = Literal["assumed", "config_alias", "probe"]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    policy: CommandPolicy
    assumption: str | None = None
    config_key: str | None = None
    probe_lines: tuple[str, ...] = ()


_REGISTRY: dict[str, CommandSpec] = {
    "source": CommandSpec(
        name="source",
        policy="assumed",
        assumption="session_established",
    ),
    "true": CommandSpec(
        name="true",
        policy="assumed",
        assumption="hush_required",
    ),
    "if": CommandSpec(
        name="if",
        policy="assumed",
        assumption="hush_required",
    ),
    "echo": CommandSpec(
        name="echo",
        policy="assumed",
        assumption="framework_echo",
    ),
    "cmdtftp": CommandSpec(
        name="cmdtftp",
        policy="config_alias",
        config_key="cmdtftp",
    ),
    "cmdtftpput": CommandSpec(
        name="cmdtftpput",
        policy="config_alias",
        config_key="cmdtftpput",
    ),
    "env export": CommandSpec(
        name="env export",
        policy="probe",
        probe_lines=("env export -t {rambase}",),
    ),
    "sf probe": CommandSpec(
        name="sf probe",
        policy="probe",
        probe_lines=("sf probe 0",),
    ),
    "sf read": CommandSpec(
        name="sf read",
        policy="probe",
        probe_lines=("sf read {rambase} 0x0 0x1",),
    ),
    # FIXME
    "sf write": CommandSpec(
        name="sf write",
        policy="probe",
        probe_lines=("sf write {rambase} 0x0 0x0",),
    ),
    # FIXME
    "sf erase": CommandSpec(
        name="sf erase",
        policy="probe",
        probe_lines=("sf erase 0x0 0x0",),
    ),
    "setexpr": CommandSpec(
        name="setexpr",
        policy="probe",
        probe_lines=("setexpr __uboot_tftp_probe 1 + 1",),
    ),
    "cp": CommandSpec(
        name="cp",
        policy="probe",
        probe_lines=("cp.b {rambase} {rambase} 0x1",),
    ),
    "crc32": CommandSpec(
        name="crc32",
        policy="probe",
        probe_lines=("crc32 {rambase} 0x1",),
    ),
    "dhcp": CommandSpec(
        name="dhcp",
        policy="probe",
        probe_lines=("dhcp",),
    ),
    "boot": CommandSpec(
        name="boot",
        policy="probe",
        probe_lines=("boot",),
    ),
    "test": CommandSpec(
        name="test",
        policy="probe",
        probe_lines=("test 0 -eq 0",),
    ),
    "mw": CommandSpec(
        name="mw",
        policy="probe",
        probe_lines=("mw.b {rambase} 0x0 0x0",),
    ),
    "setenv": CommandSpec(
        name="setenv",
        policy="probe",
        probe_lines=("setenv __uboot_tftp_probe",),
    ),
    "tftpboot": CommandSpec(
        name="tftpboot",
        policy="probe",
        probe_lines=("tftpboot {rambase} _null",),
    ),
    "tftp": CommandSpec(
        name="tftp",
        policy="probe",
        probe_lines=("tftp {rambase} _null",),
    ),
    "tftpput": CommandSpec(
        name="tftpput",
        policy="probe",
        probe_lines=("tftpput {rambase} 0x0 _null",),
    ),
    "reset": CommandSpec(
        name="reset",
        policy="probe",
        probe_lines=("reset",),
    ),
}

_FRAMEWORK_REQUIRED_COMMANDS = ("source", "true", "if", "echo", "cmdtftp")

_FRAMEWORK_EMITTED_COMMANDS = (
    "boot",
    "cmdtftp",
    "cmdtftpput",
    "cp",
    "crc32",
    "dhcp",
    "echo",
    "env export",
    "if",
    "mw",
    "reset",
    "setenv",
    "setexpr",
    "sf erase",
    "sf probe",
    "sf read",
    "sf write",
    "source",
    "test",
    "tftp",
    "tftpboot",
    "tftpput",
    "true",
)


def command_registry() -> dict[str, CommandSpec]:
    return dict(_REGISTRY)


def framework_required_commands() -> list[str]:
    return list(_FRAMEWORK_REQUIRED_COMMANDS)


def framework_emitted_commands() -> list[str]:
    return list(_FRAMEWORK_EMITTED_COMMANDS)


def normalize_requested_commands(cmd_list: list[str], session_env: dict[str, str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in cmd_list:
        canonical = _normalize_command(raw, session_env)
        if canonical in seen:
            continue
        normalized.append(canonical)
        seen.add(canonical)
    return normalized


def build_probe_batch(
    commands: list[str],
    session_env: dict[str, str],
) -> tuple[list[str], list[str], dict[str, str]]:
    lines: list[str] = []
    keys: list[str] = []
    key_map: dict[str, str] = {}
    rambase_var = session_env.get("rambase", "").strip()
    if not rambase_var:
        raise ValueError("missing session rambase for command probes")
    rambase = f"${{{rambase_var}}}"
    for index, command in enumerate(commands):
        spec = get_command_spec(command)
        if spec.policy != "probe":
            raise ValueError(f"command {command!r} is not probeable")
        if not spec.probe_lines:
            raise ValueError(f"command {command!r} is missing probe lines")
        key = f"_{index}"
        lines.extend(line.format(rambase=rambase) for line in spec.probe_lines)
        lines.append(f"setenv {key} $?")
        keys.append(key)
        key_map[command] = key
    return lines, keys, key_map


def get_command_spec(command: str) -> CommandSpec:
    try:
        return _REGISTRY[command]
    except KeyError as error:
        raise ValueError(f"unknown framework command: {command!r}") from error


def _normalize_command(command: str, session_env: dict[str, str]) -> str:
    text = str(command).strip()
    if not text:
        raise ValueError("command names must not be empty")
    if text in _REGISTRY and _REGISTRY[text].policy == "config_alias":
        alias = _REGISTRY[text]
        if alias.config_key is None:
            raise ValueError(f"alias command {text!r} is missing config_key")
        resolved = session_env.get(alias.config_key, "").strip()
        if not resolved:
            raise ValueError(f"missing session config for alias {text!r}")
        return _normalize_command(resolved, session_env)

    tokens = shlex.split(text, posix=True)
    if not tokens:
        raise ValueError("command names must not be empty")

    head = tokens[0]
    if head == "env" and len(tokens) > 1 and tokens[1] == "export":
        return "env export"
    if head == "sf" and len(tokens) > 1:
        return f"sf {tokens[1]}"
    if head.startswith("setexpr"):
        return "setexpr"
    if head.startswith("cp"):
        return "cp"
    if head.startswith("mw"):
        return "mw"
    return head
