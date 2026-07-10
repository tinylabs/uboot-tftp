"""Framework command registry and capability probe helpers."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Literal

CommandPolicy = Literal["assumed", "config_alias", "probe"]


@dataclass(frozen=True)
class CommandSpec:
    policy: CommandPolicy
    assumption: str | None = None
    config_key: str | None = None
    probe_lines: tuple[str, ...] = ()


_REGISTRY: dict[str, CommandSpec] = {
    "source": CommandSpec(
        policy="assumed",
        assumption="session_established",
    ),
    "true": CommandSpec(
        policy="assumed",
        assumption="hush_required",
    ),
    "if": CommandSpec(
        policy="assumed",
        assumption="hush_required",
    ),
    "echo": CommandSpec(
        policy="assumed",
        assumption="framework_echo",
    ),
    "cmdtftp": CommandSpec(
        policy="config_alias",
        config_key="cmdtftp",
    ),
    "cmdtftpput": CommandSpec(
        policy="config_alias",
        config_key="cmdtftpput",
    ),
    "env export": CommandSpec(
        policy="probe",
        probe_lines=("env export -t {rambase}",),
    ),
    "sf probe": CommandSpec(
        policy="probe",
        probe_lines=("sf probe 0",),
    ),
    "sf read": CommandSpec(
        policy="probe",
        probe_lines=("sf read {rambase} 0x0 0x1",),
    ),
    # FIXME
    "sf write": CommandSpec(
        policy="probe",
        probe_lines=("sf probe 0",),
    ),
    # FIXME
    "sf erase": CommandSpec(
        policy="probe",
        probe_lines=("sf probe 0",),
    ),
    "setexpr": CommandSpec(
        policy="probe",
        probe_lines=("setexpr __uboot_tftp_probe 1 + 1",),
    ),
    "cp": CommandSpec(
        policy="probe",
        probe_lines=("cp.b {rambase} {rambase} 0x1",),
    ),
    "crc32": CommandSpec(
        policy="probe",
        probe_lines=("crc32 {rambase} 0x1",),
    ),
    "dhcp": CommandSpec(
        policy="probe",
        probe_lines=("dhcp",),
    ),
    "boot": CommandSpec(
        policy="probe",
        probe_lines=("boot",),
    ),
    "test": CommandSpec(
        policy="probe",
        probe_lines=("test 0 -eq 0",),
    ),
    "mw": CommandSpec(
        policy="probe",
        probe_lines=("mw.b {rambase} 0x0 0x0",),
    ),
    "setenv": CommandSpec(
        policy="assumed",
        assumption="framework_setenv",
    ),
    "tftpboot": CommandSpec(
        policy="probe",
        probe_lines=("tftpboot {rambase} _null",),
    ),
    "tftp": CommandSpec(
        policy="probe",
        probe_lines=("tftp {rambase} _null",),
    ),
    "tftpput": CommandSpec(
        policy="probe",
        probe_lines=("tftpput {rambase} 0x0 _null",),
    ),
    "reset": CommandSpec(
        policy="probe",
        probe_lines=("reset",),
    ),
    "bootflow list": CommandSpec(
        policy="probe",
        probe_lines=("bootflow list",),
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
    *,
    key_prefix: str = "_",
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
        key = f"{key_prefix}{index}"
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

    for name in _registry_names_by_prefix_length():
        prefix = tuple(name.split())
        if tuple(tokens[: len(prefix)]) == prefix:
            return name

    head = tokens[0]
    if head.startswith("setexpr"):
        return "setexpr"
    if head.startswith("cp"):
        return "cp"
    if head.startswith("mw"):
        return "mw"
    if len(tokens) > 1:
        return " ".join(tokens)
    return head


def _registry_names_by_prefix_length() -> tuple[str, ...]:
    names = [name for name in _REGISTRY if " " in name]
    return tuple(sorted(names, key=lambda name: len(name.split()), reverse=True))
