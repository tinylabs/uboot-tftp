"""Configuration loading for the daemon entry point."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SERVER_KEYS = {
    "scriptfile",
    "script",
    "rootdir",
    "static_root",
    "tftproot",
    "address",
    "port",
    "timeout",
    "retries",
    "log_level",
    "pidfile",
}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass(frozen=True)
class ScriptRoute:
    entry_func: str
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
            self.server.get("rootdir")
            or self.server.get("static_root")
            or self.server.get("tftproot")
            or "."
        )
        path = Path(str(value))
        if not path.is_absolute():
            raise ValueError("[server] rootdir must be an absolute path")
        return path

    @property
    def script_path(self) -> Path:
        value = self.server.get("scriptfile") or self.server.get("script")
        if not value:
            raise ValueError("[server] must set scriptfile")
        path = Path(str(value))
        if path.is_absolute():
            return path
        return (self.path.parent / path).resolve()


def load_daemon_config(
    path: str | Path, *, rootdir: str | Path | None = None
) -> DaemonConfig:
    config_path = Path(path)
    data = _load_toml(config_path)

    server = dict(data.get("server", {}))
    if rootdir is not None:
        server["rootdir"] = str(rootdir)
    _validate_server_config(server)
    env = {str(key): str(value) for key, value in dict(data.get("env", {})).items()}
    _validate_base_env(env)
    default_section = dict(data.get("default", {}))
    default_entry_func = str(default_section.get("entry_func", "default"))
    default_env = {
        str(key): str(value)
        for key, value in default_section.items()
        if str(key) != "entry_func"
    }

    routes: dict[str, ScriptRoute] = {}
    for section, values in data.items():
        if section in {"server", "env", "default"}:
            continue
        route = dict(values)
        if "entry_func" not in route:
            raise ValueError(f"[{section}] must set entry_func")
        route_env = {
            str(key): str(value)
            for key, value in route.items()
            if str(key) != "entry_func"
        }
        routes[str(section).lower()] = ScriptRoute(
            entry_func=str(route["entry_func"]),
            env=route_env,
        )

    return DaemonConfig(
        path=config_path.resolve(),
        server=server,
        env=env,
        routes=routes,
        default=ScriptRoute(entry_func=default_entry_func, env=default_env),
    )


def check_user_script_syntax(path: str | Path) -> Path:
    script_path = Path(path).resolve()
    try:
        source = script_path.read_text()
    except FileNotFoundError as error:
        raise ValueError(f"script file not found: {script_path}") from error
    try:
        ast.parse(source, filename=str(script_path))
    except SyntaxError as error:
        line = f" line {error.lineno}" if error.lineno is not None else ""
        raise ValueError(
            f"python syntax error in {script_path}{line}: {error.msg}"
        ) from error
    return script_path


def _validate_base_env(env: dict[str, str]) -> None:
    required = ("rambase", "cmdtftp", "cmdtftpput")
    missing = [key for key in required if key not in env]
    if missing:
        names = ", ".join(missing)
        raise ValueError(f"[env] must define: {names}")


def _validate_server_config(server: dict[str, Any]) -> None:
    unknown = sorted(str(key) for key in server if str(key) not in _SERVER_KEYS)
    if unknown:
        raise ValueError(f"[server] unknown keys: {', '.join(unknown)}")

    _validate_non_empty_string(server, "scriptfile")
    _validate_non_empty_string(server, "script")
    _validate_non_empty_string(server, "address")
    _validate_non_empty_string(server, "pidfile")

    _validate_int_range(server, "port", minimum=1, maximum=65535)
    _validate_int_range(server, "timeout", minimum=1)
    _validate_int_range(server, "retries", minimum=0)
    _validate_log_level(server)

    _validate_server_paths(server)


def _validate_server_paths(server: dict[str, Any]) -> None:
    root = server.get("rootdir") or server.get("static_root") or server.get("tftproot")
    if root is None:
        return
    if not Path(str(root)).is_absolute():
        raise ValueError("[server] rootdir must be an absolute path")


def _validate_non_empty_string(server: dict[str, Any], key: str) -> None:
    value = server.get(key)
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"[server] {key} must be a non-empty string")


def _validate_int_range(
    server: dict[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> None:
    value = server.get(key)
    if value is None:
        return
    if not isinstance(value, int):
        raise ValueError(f"[server] {key} must be an integer")
    if value < minimum:
        raise ValueError(f"[server] {key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"[server] {key} must be <= {maximum}")


def _validate_log_level(server: dict[str, Any]) -> None:
    value = server.get("log_level")
    if value is None:
        return
    if not isinstance(value, str) or value.upper() not in _LOG_LEVELS:
        levels = ", ".join(sorted(_LOG_LEVELS))
        raise ValueError(f"[server] log_level must be one of: {levels}")


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
