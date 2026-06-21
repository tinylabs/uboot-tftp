"""Config-driven script provider for daemon use."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from .config import DaemonConfig
from .mkimage import LegacyScriptImageCompiler
from .protocol import ClientMessage, parse_client_filename
from .providers import ContentRequest, ContentResult, DynamicContentProvider
from .uploads import InMemoryUploadStore

ScriptFunction = Callable[["ClientHandle", str, str], None]


class EnvRequestNeeded(Exception):
    """Raised internally when a script needs the target env uploaded first."""


class ClientHandle:
    """Small API exposed to user script functions."""

    def __init__(
        self,
        *,
        provider: ScriptedConfigProvider,
        message: ClientMessage,
        path: str,
    ) -> None:
        self._provider = provider
        self._message = message
        self._path = path
        self._script: str | None = None
        self._reply: bool = False

    def get_env(self) -> dict[str, str]:
        return self._provider.get_env(self._message.client_id, self._path)

    def send(self, script: str) -> None:
        self._script = script
        self._reply = True

    def send_noreply(self, script: str) -> None:
        self._script = script
        self._reply = False

    @property
    def response(self) -> tuple[str, bool]:
        return self._script or "", self._reply


class ScriptedConfigProvider(DynamicContentProvider):
    """Run configured Python functions for `id=<client>/<path>` RRQs."""

    def __init__(
        self,
        config: DaemonConfig,
        *,
        upload_store: InMemoryUploadStore,
        compiler: LegacyScriptImageCompiler | None = None,
    ) -> None:
        self.config = config
        self.upload_store = upload_store
        self.compiler = compiler or LegacyScriptImageCompiler()
        self._module = _load_script_module(_script_path(config))
        self._env_upload_count: dict[str, int] = {}
        self._target_env: dict[str, dict[str, str]] = {}

    def fetch(self, request: ContentRequest) -> ContentResult:
        message = parse_client_filename(request.filename)
        self._refresh_env(message.client_id)
        path = _message_path(message)
        handle = ClientHandle(provider=self, message=message, path=path)
        function = self._function_for(message.client_id)

        try:
            function(handle, message.client_id, path)
        except EnvRequestNeeded:
            script = self._env_request_script(message.client_id, path)
        else:
            script_body, reply = handle.response
            script = (
                self._wrap_script(script_body, message.client_id, path)
                if reply
                else script_body
            )

        return ContentResult.from_bytes(self.compiler.compile(_ensure_newline(script)))

    def get_env(self, client_id: str, path: str) -> dict[str, str]:
        self._refresh_env(client_id)
        if client_id not in self._target_env:
            raise EnvRequestNeeded
        merged = dict(self.config.env)
        merged.update(self._target_env[client_id])
        return merged

    def _function_for(self, client_id: str) -> ScriptFunction:
        route = self.config.routes.get(client_id.lower(), self.config.default)
        function = getattr(self._module, route.script, None)
        if not callable(function):
            raise ValueError(f"script function not found: {route.script}")
        return function

    def _refresh_env(self, client_id: str) -> None:
        uploads = self.upload_store.by_client_id.get(client_id, [])
        if len(uploads) == self._env_upload_count.get(client_id, 0):
            return
        for upload in uploads[self._env_upload_count.get(client_id, 0) :]:
            if upload.filename.endswith("/upload/env.txt"):
                self._target_env[client_id] = _parse_env_export(upload.body)
        self._env_upload_count[client_id] = len(uploads)

    def _env_request_script(self, client_id: str, path: str) -> str:
        ramref = self._ramref()
        body = (
            f"env export -t {ramref}\n"
            f'if tftpput {ramref} ${{filesize}} "${{serverip}}:'
            f'id={client_id}/upload/env.txt"; then\n'
            f"{self._continue_call(client_id, path)}\n"
            "else echo \"openipc-tftp: environment upload failed\"; fi"
        )
        return body

    def _wrap_script(self, script: str, client_id: str, path: str) -> str:
        return f"{script.rstrip()}\n{self._continue_call(client_id, path)}"

    def _continue_call(self, client_id: str, path: str) -> str:
        ramref = self._ramref()
        return (
            f'if {self._cmdtftp()} {ramref} "${{serverip}}:'
            f'id={client_id}{path}"; then source {ramref}; '
            'else echo "openipc-tftp: stopping because tftpboot failed"; fi'
        )

    def _cmdtftp(self) -> str:
        return self.config.env.get("cmdtftp", "tftpboot")

    def _ramref(self) -> str:
        return _uboot_variable(self.config.env.get("ramvar", "baseaddr"))


def _script_path(config: DaemonConfig) -> Path:
    value = config.server.get("scriptfile") or config.server.get("script")
    if not value:
        raise ValueError("[server] must set scriptfile")
    path = Path(str(value))
    if not path.is_absolute():
        path = config.path.parent / path
    return path


def _load_script_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("openipc_tftp_user_script", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"unable to load script file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _message_path(message: ClientMessage) -> str:
    if not message.segments:
        return "/"
    return "/" + "/".join(message.segments)


def _parse_env_export(body: bytes) -> dict[str, str]:
    env: dict[str, str] = {}
    text = body.decode("utf-8", errors="replace").replace("\0", "\n")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key:
            env[key] = value
    return env


def _uboot_variable(name: str) -> str:
    if name.startswith("${") and name.endswith("}"):
        return name
    return f"${{{name}}}"


def _ensure_newline(script: str) -> str:
    return script if script.endswith("\n") else f"{script}\n"
