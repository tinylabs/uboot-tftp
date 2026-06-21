"""Configuration loading for the daemon entry point."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ScriptRoute:
    script: str
    env: dict[str, str]


@dataclass(frozen=True)
class DaemonConfig:
    path: Path
    server: dict[str, Any]
    env: dict[str, str]
    routes: dict[str, ScriptRoute]
    default: ScriptRoute

    @property
    def static_root(self) -> Path:
        value = (
            self.server.get("root")
            or self.server.get("static_root")
            or self.server.get("tftproot")
            or "."
        )
        path = Path(str(value))
        if path.is_absolute():
            return path
        return (self.path.parent / path).resolve()

    @property
    def script_path(self) -> Path:
        value = self.server.get("scriptfile") or self.server.get("script")
        if not value:
            raise ValueError("[server] must set scriptfile")
        path = Path(str(value))
        if path.is_absolute():
            return path
        return (self.path.parent / path).resolve()


def load_daemon_config(path: str | Path) -> DaemonConfig:
    config_path = Path(path)
    data = _load_toml(config_path)

    server = dict(data.get("server", {}))
    env = {str(key): str(value) for key, value in dict(data.get("env", {})).items()}
    _validate_base_env(env)
    default_section = dict(data.get("default", {}))
    default_script = str(default_section.get("script", "default"))
    default_env = {
        str(key): str(value)
        for key, value in default_section.items()
        if str(key) != "script"
    }

    routes: dict[str, ScriptRoute] = {}
    for section, values in data.items():
        if section in {"server", "env", "default"}:
            continue
        route = dict(values)
        if "script" not in route:
            raise ValueError(f"[{section}] must set script")
        route_env = {
            str(key): str(value) for key, value in route.items() if str(key) != "script"
        }
        routes[str(section).lower()] = ScriptRoute(
            script=str(route["script"]),
            env=route_env,
        )

    return DaemonConfig(
        path=config_path.resolve(),
        server=server,
        env=env,
        routes=routes,
        default=ScriptRoute(script=default_script, env=default_env),
    )


def _validate_base_env(env: dict[str, str]) -> None:
    required = ("rambase", "cmdtftp", "cmdtftpput")
    missing = [key for key in required if key not in env]
    if missing:
        names = ", ".join(missing)
        raise ValueError(f"[env] must define: {names}")


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:
        return _load_simple_toml(path)

    with path.open("rb") as fileobj:
        return tomllib.load(fileobj)


def _load_simple_toml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    section: dict[str, Any] | None = None
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if not name:
                raise ValueError(f"empty section name on line {line_number}")
            section = data.setdefault(name, {})
            continue
        if section is None:
            raise ValueError(f"key outside section on line {line_number}")
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError(f"expected key=value on line {line_number}")
        section[key.strip()] = _parse_simple_toml_value(value.strip(), line_number)
    return data


def _parse_simple_toml_value(value: str, line_number: int) -> Any:
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError) as error:
        raise ValueError(f"unsupported TOML value on line {line_number}") from error
