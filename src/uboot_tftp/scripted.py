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
from .download_jobs import DownloadArtifact, DownloadJobStore
from .mkimage import LegacyScriptImageCompiler
from .protocol import ParsedPath, parse_request_path
from .providers import ContentRequest, ContentResult, DynamicContentProvider
from .sessions import ClientSession, InMemorySessionStore, PendingReceive
from .ubootcmds import (
    build_probe_batch,
    framework_required_commands,
    get_command_spec,
    normalize_requested_commands,
)
from .ubootterm import uboot_err, uboot_msg, uboot_term_reset
from .ubootenv import ubootenv_parse_export
from .uploads import InMemoryUploadStore


class ReceiveFailedError(RuntimeError):
    """Raised into the user handler when a requested WRQ was not received."""


@dataclass(frozen=True)
class _ExecutionRequest:
    script: str
    final: bool
    receive_size: int | None = None
    return_keys: tuple[str, ...] = ()
    receive_offset: int | str | None = None

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
    def server_ip(self) -> str:
        return self.session.server_ip

    @property
    def cmdtftp(self) -> str:
        return self.session.env["cmdtftp"]

    @property
    def rambase_var(self) -> str:
        return self.session.env["rambase"]

    @property
    def rambase(self) -> str:
        return f"${{{self.rambase_var}}}"

    @property
    def rambase_addr(self) -> int:
        value = self.session.env.get(self.rambase_var)
        if value is None:
            raise RuntimeError(f"resolved RAM base value is missing for {self.rambase_var!r}")
        try:
            return int(value, 0)
        except ValueError as error:
            raise ValueError(
                f"invalid RAM base value for {self.rambase_var!r}: {value!r}"
            ) from error

    @property
    def root(self) -> str:
        return str(self.provider.static_root)

    def get_config(self, key: str, default: str | None = None) -> str:
        if default is not None:
            return self.session.env.get(key, default)
        return self.session.env[key]

    @property
    def env(self) -> dict[str, str]:
        return self.session.public_env

    @property
    def is_le(self) -> bool:
        if self.session.is_le is None:
            raise RuntimeError("endianness preflight has not completed")
        return self.session.is_le

    async def exec(
        self,
        script: str | Iterable[str],
        *,
        final: bool = False,
        keys: Iterable[str] = (),
    ) -> None:
        await _ExecutionAwaitable(
            _ExecutionRequest(
                script=_join_script_lines(script),
                final=final,
                return_keys=_normalize_return_keys(keys),
            )
        )

    async def exec_recv(
        self,
        script: str | Iterable[str],
        size: int,
        *,
        final: bool = False,
        keys: Iterable[str] = (),
        offset: int | str | None = None,
    ) -> bytes:
        if final:
            raise ValueError("exec_recv(..., final=True) is not supported")
        result = await _ExecutionAwaitable(
            _ExecutionRequest(
                script=_join_script_lines(script),
                final=False,
                receive_size=size,
                return_keys=_normalize_return_keys(keys),
                receive_offset=offset,
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

    def file_exists(self, filename: str | Path) -> bool:
        target = _resolve_static_path(self.provider.static_root, "/" + str(filename))
        return bool(target is not None and target.exists())

    def read_file(self, filename: str | Path) -> bytes:
        target = _resolve_static_path(self.provider.static_root, "/" + str(filename))
        if target is None:
            raise ValueError(f"unsafe filename path: {filename!r}")
        if not target.exists():
            raise FileNotFoundError(f"missing file: {filename!r}")
        return target.read_bytes()

    def parse_env_export(self, body: bytes) -> dict[str, str]:
        return ubootenv_parse_export(body)

    def acquire_download(
        self,
        *,
        artifact_key: str,
        url: str,
        destination: str | Path,
        page_url: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> DownloadArtifact:
        target = _resolve_static_path(self.provider.static_root, "/" + str(destination))
        if target is None:
            raise ValueError(f"unsafe destination path: {destination!r}")
        relative_path = str(Path(str(destination).lstrip("/"))).replace("\\", "/")
        artifact = self.provider.download_jobs.acquire(
            artifact_key=artifact_key,
            session_id=self.session.client_id,
            url=url,
            relative_path=relative_path,
            final_path=target,
            page_url=page_url,
            headers=headers,
        )
        self.session.download_artifacts.add(artifact_key)
        return artifact

    def get_download(self, artifact_key: str) -> DownloadArtifact:
        artifact = self.provider.download_jobs.get(artifact_key)
        if artifact is None:
            raise FileNotFoundError(f"unknown download artifact: {artifact_key!r}")
        return artifact

    async def fetch_env(
        self,
        *,
        export_script: str | Iterable[str] | None = None,
        upload_script: str | Iterable[str] = ("echo uploading environment snapshot",),
        size_key: str = "filesize",
    ) -> dict[str, str]:
        export_lines = (
            export_script if export_script is not None else [f"env export -t {self.rambase}"]
        )
        await self.exec(export_lines, keys=[size_key])
        size_text = self.env.get(size_key)
        if size_text is None:
            raise ValueError(f"missing {size_key!r} after environment export")
        try:
            size = _parse_uboot_number(size_text)
        except ValueError as error:
            raise ValueError(f"invalid {size_key!r} value: {size_text!r}") from error
        data = await self.exec_recv(upload_script, size)
        return ubootenv_parse_export(data)

    async def check_cmds(self, cmd_list: list[str]) -> list[str]:
        combined = [*cmd_list, *framework_required_commands()]
        commands = normalize_requested_commands(combined, self.session.env)
        session_proven = set(normalize_requested_commands(["cmdtftp"], self.session.env))

        probe_list: list[str] = []
        for command in commands:
            if command in session_proven:
                self.session.unsupported_cmds.discard(command)
                self.session.supported_cmds.add(command)
                continue
            if command in self.session.supported_cmds:
                continue
            if command in self.session.unsupported_cmds:
                continue
            spec = get_command_spec(command)
            if spec.policy == "assumed":
                self.session.supported_cmds.add(command)
                continue
            if spec.policy == "probe":
                probe_list.append(command)
                continue
            raise ValueError(f"unexpected command policy for {command!r}: {spec.policy}")

        if probe_list:
            script, keys, key_map = build_probe_batch(probe_list, self.session.env)
            await self.exec(script, keys=keys)
            for command in probe_list:
                if self.session.env.get(key_map[command]) == "0":
                    self.session.supported_cmds.add(command)
                else:
                    self.session.unsupported_cmds.add(command)

        return [command for command in commands if command in self.session.supported_cmds]

class ScriptedSessionProvider(DynamicContentProvider):
    """Serve static files or dispatch session RRQs to user handlers."""

    def __init__(
        self,
        config: DaemonConfig,
        *,
        sessions: InMemorySessionStore | None = None,
        upload_store: InMemoryUploadStore | None = None,
        compiler: LegacyScriptImageCompiler | None = None,
        download_jobs: DownloadJobStore | None = None,
    ) -> None:
        self.config = config
        self.sessions = sessions or InMemorySessionStore()
        self.upload_store = upload_store or InMemoryUploadStore(self.sessions)
        self.compiler = compiler or LegacyScriptImageCompiler()
        self._module = _load_script_module(config.script_path)
        self.static_root = config.static_root
        self.static_root.mkdir(parents=True, exist_ok=True)
        self.download_jobs = download_jobs or DownloadJobStore(
            temp_root=self.static_root / ".downloads"
        )

    def fetch(self, request: ContentRequest) -> ContentResult:
        parsed = parse_request_path(request.filename)
        if not parsed.is_session:
            return self._static_result(parsed.path)
        if _is_continuation(parsed):
            return self._resume_session(request, parsed)
        return self._start_session(request, parsed)

    def _start_session(self, request: ContentRequest, parsed: ParsedPath) -> ContentResult:
        existing = self.sessions.get(parsed.client_id)
        if existing is not None:
            self._discard_session(existing)
        session = self.sessions.replace(parsed.client_id)
        session.record_rrq(parsed)
        session.server_ip = _get_local_ip(str(request.peer[0]))
        route = self._route_for(parsed.client_id)
        session.env = _session_env(self.config.env, route.env, parsed)
        session.public_env = _public_env(session.env)
        session.preflight_pending = True
        return self._preflight_result(session)

    def _resume_session(self, request: ContentRequest, parsed: ParsedPath) -> ContentResult:
        session = self.sessions.require(parsed.client_id)
        if parsed.values.get("token") != session.current_token:
            raise FileNotFoundError(f"invalid session token for {parsed.client_id!r}")
        session.record_rrq(parsed)
        _merge_continuation_values(session, parsed)
        if session.preflight_pending:
            session.preflight_pending = False
            if session.env.get("hush_shell") != "true":
                session.phase = "complete"
                self._discard_session(session)
                return ContentResult.from_bytes(
                    self.compiler.compile(_ensure_newline(_hush_failure_script()))
                )
            session.is_le = session.env.get("_1") == "44"
            session.handler = self._create_handler(session, request)
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
            self._discard_session(session)
            raise FileNotFoundError("session completed without emitting a script")
        except ReceiveFailedError as error:
            session.phase = "complete"
            self._discard_session(session)
            raise FileNotFoundError(str(error)) from error
        return self._result_from_instruction(session, instruction)

    def _create_handler(
        self,
        session: ClientSession,
        request: ContentRequest,
    ):
        initial_request = session.requests[0]
        route = self._route_for(session.client_id)
        handle = SessionHandle(
            provider=self,
            session=session,
            parsed=initial_request,
            request=request,
        )
        function = getattr(self._module, route.entry_func, None)
        if not callable(function):
            raise ValueError(f"script function not found: {route.entry_func}")
        handler = function(
            handle,
            session.client_id,
            _command_from_segments(initial_request.segments),
            session.public_env,
        )
        if not inspect.iscoroutine(handler):
            raise TypeError("session handlers must be async functions")
        return handler

    def _preflight_result(self, session: ClientSession) -> ContentResult:
        session.current_token = _new_token()
        session.phase = "await_rrq"
        script = self._append_continue(
            _preflight_probe_script(session.env["rambase"]),
            session,
            recv_status=None,
            return_keys=("hush_shell", "_1", session.env["rambase"]),
        )
        return ContentResult.from_bytes(self.compiler.compile(_ensure_newline(script)))

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
            script = self._append_receive(
                script,
                session,
                instruction.return_keys,
                instruction.receive_offset,
            )
        elif instruction.final:
            session.phase = "complete"
            self._discard_session(session)
        else:
            session.current_token = _new_token()
            session.phase = "await_rrq"
            script = self._append_continue(
                script,
                session,
                recv_status=None,
                return_keys=instruction.return_keys,
            )
        return ContentResult.from_bytes(self.compiler.compile(_ensure_newline(script)))

    def _append_continue(
        self,
        script: str,
        session: ClientSession,
        *,
        recv_status: str | None,
        return_keys: tuple[str, ...] = (),
    ) -> str:
        command = self._continue_command(
            session,
            recv_status=recv_status,
            return_keys=return_keys,
        )
        return _join_script_lines((script, command))

    def _continue_command(
        self,
        session: ClientSession,
        *,
        recv_status: str | None,
        return_keys: tuple[str, ...] = (),
    ) -> str:
        if session.current_token is None:
            raise RuntimeError("missing continuation token")
        path = f"id={session.client_id}/token={session.current_token}"
        if recv_status is not None:
            path = f"{path}/recv={recv_status}"
        path = _append_return_keys(path, return_keys)
        command = (
            f'if {session.env["cmdtftp"]} ${{{session.env["rambase"]}}} '
            f'"{session.server_ip}:{path}"; '
            f'then source ${{{session.env["rambase"]}}}; '
            'else echo "uboot-tftp: continuation RRQ failed"; fi'
        )
        return command

    def _append_receive(
        self,
        script: str,
        session: ClientSession,
        return_keys: tuple[str, ...],
        receive_offset: int | str | None,
    ) -> str:
        pending = session.pending_receive
        if pending is None:
            raise RuntimeError("missing pending receive state")
        upload_remote = f"id={session.client_id}/token={pending.token}{pending.upload_path}"
        success = self._continue_command(
            session=session,
            recv_status="ok",
            return_keys=return_keys,
        )
        failure = self._continue_command(
            session=session,
            recv_status="failed",
            return_keys=return_keys,
        )
        upload_address, prelude, cleanup = _receive_address(session, receive_offset)
        receive = (
            f'if {session.env["cmdtftpput"]} {upload_address} {_format_uboot_number(pending.size)} '
            f'"{session.server_ip}:{upload_remote}";\n'
            f"then\n"
            f"    {success}\n"
            f"else\n"
            f"    {failure}\n"
            f"fi"
        )
        return _join_script_lines((script, prelude, receive, cleanup))

    def _route_for(self, client_id: str | None):
        return self.config.default if client_id is None else self.config.routes.get(
            client_id.lower(), self.config.default
        )

    def _discard_session(self, session: ClientSession) -> None:
        for artifact_key in list(session.download_artifacts):
            self.download_jobs.release(
                artifact_key=artifact_key,
                session_id=session.client_id,
            )
            session.download_artifacts.discard(artifact_key)
        self.sessions.discard(session.client_id)

    def _static_result(self, path: str) -> ContentResult:
        file_path = _resolve_static_path(self.static_root, path)
        if file_path is None or not file_path.is_file():
            raise FileNotFoundError(path)
        return ContentResult.from_bytes(file_path.read_bytes())


def _load_script_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("uboot_tftp_user_script", path)
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


def _preflight_probe_script(rambase_var: str) -> str:
    rambase = f"${{{rambase_var}}}"
    return _join_script_lines(
        (
            uboot_term_reset(),
            uboot_msg("Executing preflight... ", bold=True),
            "if true; then setenv hush_shell true; fi",
            f"setexpr.l tmp *{rambase}",
            f"mw.l {rambase} 0x11223344 1",
            f"setexpr.b _1 *{rambase}",
            f"mw.l {rambase} ${{tmp}} 1",
            "setenv tmp",
        )
    )


def _hush_failure_script() -> str:
    return _join_script_lines(
        (
            uboot_err("U-Boot hush shell is required"),
            uboot_msg("uboot-tftp requires hush-compatible if/then support", color="yellow"),
        )
    )


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


def _merge_continuation_values(session: ClientSession, parsed: ParsedPath) -> None:
    for key, value in parsed.values.items():
        if key in {"token", "recv"}:
            continue
        session.env[key] = value
        if key not in {"rambase", "cmdtftp", "cmdtftpput"}:
            session.public_env[key] = value


def _normalize_return_keys(keys: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for key in keys:
        name = str(key)
        if not name or any(character in name for character in "/=\r\n"):
            raise ValueError(f"invalid return key: {key!r}")
        normalized.append(name)
    return tuple(normalized)


def _append_return_keys(path: str, keys: tuple[str, ...]) -> str:
    for key in keys:
        path = f"{path}/{key}=${{{key}}}"
    return path


def _receive_address(
    session: ClientSession,
    offset: int | str | None,
) -> tuple[str, str | None, str | None]:
    if offset is None:
        return (f'${{{session.env["rambase"]}}}', None, None)
    tmp_name = _new_tmp_name("recv")
    prelude = (
        f"setexpr {tmp_name} ${{{session.env['rambase']}}} + "
        f"{_format_uboot_number(offset)}"
    )
    cleanup = f"setenv {tmp_name}"
    return (f"${{{tmp_name}}}", prelude, cleanup)


def _format_uboot_number(value: int | str) -> str:
    if isinstance(value, int):
        return hex(value)
    return value


def _parse_uboot_number(value: str) -> int:
    text = value.strip()
    if text.lower().startswith("0x"):
        return int(text, 16)
    return int(text, 16)


def _get_local_ip(peer_hint: str) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((peer_hint if "." in peer_hint else "8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _new_token() -> str:
    return secrets.token_urlsafe(8)


def _new_tmp_name(kind: str) -> str:
    return f"__uboot_tftp_{kind}_{secrets.token_hex(4)}"


class _ExecutionAwaitable:
    def __init__(self, request: _ExecutionRequest) -> None:
        self.request = request

    def __await__(self):
        result = yield self.request
        return result
