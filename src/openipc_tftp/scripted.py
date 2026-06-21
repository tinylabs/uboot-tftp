"""Minimal config-driven session provider."""

from __future__ import annotations

import importlib.util
import inspect
import secrets
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from .config import DaemonConfig
from .mkimage import LegacyScriptImageCompiler
from .protocol import ParsedPath, parse_request_path
from .providers import ContentRequest, ContentResult, DynamicContentProvider
from .sessions import ClientSession, InMemorySessionStore, PendingReceive
from .uploads import InMemoryUploadStore


class ReceiveFailedError(RuntimeError):
    """Raised into the user handler when a requested WRQ was not received."""


@dataclass(frozen=True)
class _ExecutionRequest:
    script: str
    final: bool
    receive_size: int | None = None

    @property
    def expects_upload(self) -> bool:
        return self.receive_size is not None


class SessionHandle:
    """Small API exposed to user-defined handler functions."""

    def __init__(
        self,
        *,
        provider: "ScriptedSessionProvider",
        session: ClientSession,
        parsed: ParsedPath,
        request: ContentRequest,
    ) -> None:
        self.provider = provider
        self.session = session
        self.parsed = parsed
        self.request = request

    @property
    def ident(self) -> str:
        return self.session.client_id

    @property
    def rambase_var(self) -> str:
        return self.session.env["rambase"]

    @property
    def rambase(self) -> str:
        return f"${{{self.rambase_var}}}"

    def get_config(self, key: str, default: str | None = None) -> str:
        if default is not None:
            return self.session.env.get(key, default)
        return self.session.env[key]

    async def exec(
        self,
        script: str | Iterable[str],
        *,
        final: bool = False,
    ) -> None:
        await _ExecutionAwaitable(
            _ExecutionRequest(script=_join_script_lines(script), final=final)
        )

    async def exec_recv(
        self,
        script: str | Iterable[str],
        sz: int,
        *,
        final: bool = False,
    ) -> bytes:
        if final:
            raise ValueError("exec_recv(..., final=True) is not supported")
        result = await _ExecutionAwaitable(
            _ExecutionRequest(
                script=_join_script_lines(script),
                final=False,
                receive_size=sz,
            )
        )
        if result is None:
            raise ReceiveFailedError("expected WRQ upload before continuation RRQ")
        return result

    def write_file(self, destination: str | Path, body: bytes) -> Path:
        target = _resolve_static_path(self.provider.static_root, "/" + str(destination))
        if target is None:
            raise ValueError(f"unsafe destination path: {destination!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return target


class ScriptedSessionProvider(DynamicContentProvider):
    """Serve static files or dispatch session RRQs to user handlers."""

    def __init__(
        self,
        config: DaemonConfig,
        *,
        sessions: InMemorySessionStore | None = None,
        upload_store: InMemoryUploadStore | None = None,
        compiler: LegacyScriptImageCompiler | None = None,
    ) -> None:
        self.config = config
        self.sessions = sessions or InMemorySessionStore()
        self.upload_store = upload_store or InMemoryUploadStore(self.sessions)
        self.compiler = compiler or LegacyScriptImageCompiler()
        self._module = _load_script_module(config.script_path)
        self.static_root = config.static_root
        self.static_root.mkdir(parents=True, exist_ok=True)

    def fetch(self, request: ContentRequest) -> ContentResult:
        parsed = parse_request_path(request.filename)
        if not parsed.is_session:
            return self._static_result(parsed.path)
        if _is_continuation(parsed):
            return self._resume_session(request, parsed)
        return self._start_session(request, parsed)

    def _start_session(self, request: ContentRequest, parsed: ParsedPath) -> ContentResult:
        session = self.sessions.replace(parsed.client_id)
        session.record_rrq(parsed)
        session.server_ip = _get_local_ip(str(request.peer[0]))
        route = self._route_for(parsed.client_id)
        session.env = dict(self.config.env)
        session.env.update(route.env)
        handle = SessionHandle(
            provider=self,
            session=session,
            parsed=parsed,
            request=request,
        )
        function = getattr(self._module, route.script, None)
        if not callable(function):
            raise ValueError(f"script function not found: {route.script}")
        session.env = _session_env(self.config.env, route.env, parsed)
        handler = function(
            handle,
            parsed.client_id,
            _command_from_segments(parsed.segments),
            _public_env(session.env),
        )
        if not inspect.iscoroutine(handler):
            raise TypeError("session handlers must be async functions")
        session.handler = handler
        return self._advance_session(session, None)

    def _resume_session(self, request: ContentRequest, parsed: ParsedPath) -> ContentResult:
        session = self.sessions.require(parsed.client_id)
        if parsed.values.get("token") != session.current_token:
            raise FileNotFoundError(f"invalid session token for {parsed.client_id!r}")
        session.record_rrq(parsed)
        if session.handler is None:
            raise FileNotFoundError(f"session has no active handler: {parsed.client_id!r}")
        if session.pending_receive is not None:
            status = parsed.values.get("recv", "failed")
            if status == "ok" and session.pending_receive.uploaded is not None:
                send_value = session.pending_receive.uploaded
            else:
                send_value = None
            session.pending_receive = None
            return self._advance_session(session, send_value)
        return self._advance_session(session, None)

    def _advance_session(
        self,
        session: ClientSession,
        send_value: bytes | None,
    ) -> ContentResult:
        try:
            if send_value is None:
                instruction = session.handler.send(None)
            else:
                instruction = session.handler.send(send_value)
        except StopIteration:
            session.phase = "complete"
            self.sessions.discard(session.client_id)
            raise FileNotFoundError("session completed without emitting a script")
        except ReceiveFailedError as error:
            session.phase = "complete"
            self.sessions.discard(session.client_id)
            raise FileNotFoundError(str(error)) from error
        return self._result_from_instruction(session, instruction)

    def _result_from_instruction(
        self,
        session: ClientSession,
        instruction: _ExecutionRequest,
    ) -> ContentResult:
        if not isinstance(instruction, _ExecutionRequest):
            raise TypeError("session handlers must await tftp.exec(...) helpers")
        script = instruction.script.rstrip()
        if instruction.expects_upload:
            token = _new_token()
            session.current_token = token
            upload_path = "/upload.bin"
            session.pending_receive = PendingReceive(
                token=token,
                upload_path=upload_path,
                size=instruction.receive_size or 0,
            )
            session.phase = "await_upload"
            script = self._append_receive(script, session)
        elif instruction.final:
            session.phase = "complete"
            self.sessions.discard(session.client_id)
        else:
            session.current_token = _new_token()
            session.phase = "await_rrq"
            script = self._append_continue(script, session, recv_status=None)
        return ContentResult.from_bytes(self.compiler.compile(_ensure_newline(script)))

    def _append_continue(
        self,
        script: str,
        session: ClientSession,
        *,
        recv_status: str | None,
    ) -> str:
        if session.current_token is None:
            raise RuntimeError("missing continuation token")
        path = f"id={session.client_id}/token={session.current_token}"
        if recv_status is not None:
            path = f"{path}/recv={recv_status}"
        command = (
            f'if {session.env["cmdtftp"]} ${{{session.env["rambase"]}}} '
            f'"{session.server_ip}:{path}"; '
            f'then source ${{{session.env["rambase"]}}}; '
            'else echo "openipc-tftp: continuation RRQ failed"; fi'
        )
        return _join_script_lines((script, command))

    def _append_receive(self, script: str, session: ClientSession) -> str:
        pending = session.pending_receive
        if pending is None:
            raise RuntimeError("missing pending receive state")
        upload_remote = f"id={session.client_id}/token={pending.token}{pending.upload_path}"
        success = self._append_continue(script="", session=session, recv_status="ok")
        failure = self._append_continue(script="", session=session, recv_status="failed")
        receive = (
            f'if {session.env["cmdtftpput"]} ${{{session.env["rambase"]}}} {pending.size} '
            f'"{session.server_ip}:{upload_remote}"; '
            f"then {success} "
            f"else {failure} fi"
        )
        return _join_script_lines((script, receive))

    def _route_for(self, client_id: str | None):
        return self.config.default if client_id is None else self.config.routes.get(
            client_id.lower(), self.config.default
        )

    def _static_result(self, path: str) -> ContentResult:
        file_path = _resolve_static_path(self.static_root, path)
        if file_path is None or not file_path.is_file():
            raise FileNotFoundError(path)
        return ContentResult.from_bytes(file_path.read_bytes())


def _load_script_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("openipc_tftp_user_script", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"unable to load script file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_continuation(parsed: ParsedPath) -> bool:
    return "token" in parsed.values


def _resolve_static_path(root: Path, path: str) -> Path | None:
    relative = Path(path.lstrip("/"))
    candidate = (root / relative).resolve()
    root = root.resolve()
    if candidate == root:
        return None
    if root not in candidate.parents:
        return None
    return candidate


def _join_script_lines(lines: str | Iterable[str]) -> str:
    if isinstance(lines, str):
        return lines
    return "\n".join(line for line in lines if line)


def _ensure_newline(script: str) -> str:
    return script if script.endswith("\n") else f"{script}\n"


def _command_from_segments(segments: tuple[str, ...]) -> str:
    return segments[0] if segments else ""


def _session_env(
    base_env: dict[str, str],
    route_env: dict[str, str],
    parsed: ParsedPath,
) -> dict[str, str]:
    merged = dict(base_env)
    merged.update(route_env)
    for segment in parsed.segments[1:]:
        key, separator, value = segment.partition("=")
        if separator == "=" and key:
            merged[key] = value
    return merged


def _public_env(env: dict[str, str]) -> dict[str, str]:
    hidden = {"rambase", "cmdtftp", "cmdtftpput"}
    return {key: value for key, value in env.items() if key not in hidden}


def _get_local_ip(peer_hint: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect((peer_hint if "." in peer_hint else "8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def _new_token() -> str:
    return secrets.token_urlsafe(8)


class _ExecutionAwaitable:
    def __init__(self, request: _ExecutionRequest) -> None:
        self.request = request

    def __await__(self):
        result = yield self.request
        return result
