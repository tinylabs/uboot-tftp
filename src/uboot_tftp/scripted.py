"""Minimal config-driven session provider."""

from __future__ import annotations

import importlib.util
import inspect
import json
import itertools
import re
import secrets
import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from .config import DaemonConfig
from .download_jobs import DownloadArtifact, DownloadJobStore
from .mkimage import LegacyScriptImageCompiler
from .protocol import ParsedPath, parse_request_path
from .providers import ContentRequest, ContentResult, DynamicContentProvider
from .sessions import (
    ClientSession,
    InMemorySessionStore,
    PendingFrameworkReturn,
    PendingReceive,
)
from .ubootcmds import (
    build_probe_batch,
    framework_required_commands,
    get_command_spec,
    normalize_requested_commands,
)
from .ubootterm import uboot_err, uboot_msg, uboot_term_reset
from .ubootenv import ubootenv_parse_export
from .ubootscript import reset_tmp_counter as reset_script_snippet_tmp_counter
from .uploads import InMemoryUploadStore


class ReceiveFailedError(RuntimeError):
    """Raised into the user handler when a requested WRQ was not received."""


@dataclass(frozen=True)
class ReturnBinding:
    session: ClientSession
    source_key: str
    logical_key: str | None = None
    public: bool = False
    clear_source: bool = True
    kind: str = "value"
    command: str | None = None

    def capture(self) -> str:
        return self.source_key

    def str(self) -> str:
        if self.logical_key is None:
            raise RuntimeError("return binding does not expose a logical key")
        return self.session.env[self.logical_key]

    def int(self) -> int:
        return int(self.str(), 0)


@dataclass(frozen=True)
class _ReturnBindings:
    keys: tuple[str, ...] = ()
    managed: tuple[ReturnBinding, ...] = ()

    def with_managed(self, *bindings: ReturnBinding) -> "_ReturnBindings":
        if not bindings:
            return self
        return _ReturnBindings(
            keys=self.keys,
            managed=(*self.managed, *bindings),
        )

    def with_keys(self, keys: Iterable[str]) -> "_ReturnBindings":
        return _ReturnBindings(
            keys=_normalize_return_keys(keys),
            managed=self.managed,
        )


@dataclass(frozen=True)
class _ExecutionRequest:
    script: str
    final: bool
    receive_size: int | None = None
    returns: _ReturnBindings = field(default_factory=_ReturnBindings)
    receive_offset: int | str | None = None
    required_cmds: tuple[str, ...] = ()

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
        returns: Iterable[ReturnBinding] = (),
        requires: Iterable[str] = (),
    ) -> bool:
        return await _ExecutionAwaitable(
            self._build_request(
                script,
                final=final,
                keys=keys,
                returns=returns,
                requires=requires,
            )
        )

    async def exec_recv(
        self,
        script: str | Iterable[str],
        size: int,
        *,
        final: bool = False,
        keys: Iterable[str] = (),
        returns: Iterable[ReturnBinding] = (),
        offset: int | str | None = None,
        requires: Iterable[str] = (),
    ) -> bytes:
        if final:
            raise ValueError("exec_recv(..., final=True) is not supported")
        result = await _ExecutionAwaitable(
            self._build_request(
                script,
                final=False,
                size=size,
                keys=keys,
                returns=returns,
                offset=offset,
                requires=requires,
            )
        )
        if result is None:
            raise ReceiveFailedError("expected WRQ upload before continuation RRQ")
        return result

    def exec_queue(self, script: Iterable[str], *, requires: Iterable[str] = ()) -> None:
        self.session.queued_scripts.extend(line for line in script if line)
        self.session.queued_required_cmds.extend(_normalize_required_cmds(requires))

    def bind(
        self,
        logical_key: str,
        *,
        source_key: str | None = None,
        public: bool = True,
    ) -> ReturnBinding:
        return _new_return_binding(
            self.session,
            logical_key=logical_key,
            source_key=source_key,
            public=public,
        )

    def _build_request(
        self,
        script: str | Iterable[str],
        *,
        final: bool,
        size: int | None = None,
        keys: Iterable[str] = (),
        returns: Iterable[ReturnBinding] = (),
        offset: int | str | None = None,
        requires: Iterable[str] = (),
    ) -> _ExecutionRequest:
        required_cmds = _normalize_required_cmds(
            (*self.session.queued_required_cmds, *tuple(requires))
        )
        body = _join_script_lines(
            (
                *self.session.queued_scripts,
                _join_script_lines(script),
            )
        )
        self.session.queued_scripts.clear()
        self.session.queued_required_cmds.clear()
        return _ExecutionRequest(
            script=body,
            final=final,
            receive_size=size,
            returns=_ReturnBindings(
                keys=_normalize_return_keys(keys),
                managed=tuple(returns),
            ),
            receive_offset=offset,
            required_cmds=required_cmds,
        )

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
        size_return = self.bind(size_key, source_key="filesize", public=True)
        await self.exec(export_lines, returns=[size_return])
        size_text = self.session.env.get(size_key)
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
            try:
                spec = get_command_spec(command)
            except ValueError:
                self.session.supported_cmds.discard(command)
                self.session.unsupported_cmds.add(command)
                continue
            if spec.policy == "assumed":
                self.session.supported_cmds.add(command)
                continue
            if spec.policy == "probe":
                probe_list.append(command)
                continue
            raise ValueError(f"unexpected command policy for {command!r}: {spec.policy}")

        if probe_list:
            probe_returns = [
                _new_return_binding(
                    self.session,
                    None,
                    source_key=f"_c{index}",
                    public=False,
                    kind="probe",
                    command=command,
                )
                for index, command in enumerate(probe_list)
            ]
            script, _, _ = build_probe_batch(
                probe_list,
                self.session.env,
                keys=[binding.capture() for binding in probe_returns],
            )
            await self.exec(script, returns=probe_returns)

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
        session_log_dir: str | Path | None = None,
    ) -> None:
        self.config = config
        self.sessions = sessions or InMemorySessionStore()
        self.upload_store = upload_store or InMemoryUploadStore(self.sessions)
        self.compiler = compiler or LegacyScriptImageCompiler()
        self._module = _load_script_module(config.script_path)
        self.static_root = config.static_root
        self.static_root.mkdir(parents=True, exist_ok=True)
        self.session_log_dir = (
            Path(session_log_dir).resolve() if session_log_dir is not None else None
        )
        if self.session_log_dir is not None:
            self.session_log_dir.mkdir(parents=True, exist_ok=True)
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
        self._start_session_log(session)
        return self._preflight_result(session, request)

    def _resume_session(self, request: ContentRequest, parsed: ParsedPath) -> ContentResult:
        session = self.sessions.require(parsed.client_id)
        if parsed.values.get("token") != session.current_token:
            raise FileNotFoundError(f"invalid session token for {parsed.client_id!r}")
        session.record_rrq(parsed)
        _merge_continuation_values(session, parsed)
        _queue_pending_cleanup(session)
        if session.preflight_pending:
            session.preflight_pending = False
            if session.env.get("hush_shell") != "true":
                session.phase = "complete"
                self._discard_session(session)
                script = _ensure_newline(_hush_failure_script())
                self._log_session_script(session, request, script)
                return ContentResult.from_bytes(self.compiler.compile(script))
            session.is_le = session.env.get("_1") == "44"
            session.queued_scripts.append(uboot_msg("OK"))
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
            return self._advance_session(session, request, send_value)
        return self._advance_session(session, request, _consume_exec_status(session))

    def _advance_session(
        self,
        session: ClientSession,
        request: ContentRequest,
        send_value: object,
    ) -> ContentResult:
        _reset_per_instruction_allocators(session)
        try:
            if send_value is _NO_EXEC_RESULT:
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
        return self._result_from_instruction(session, request, instruction)

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

    def _preflight_result(self, session: ClientSession, request: ContentRequest) -> ContentResult:
        session.current_token = _new_token()
        session.phase = "await_rrq"
        preflight_returns = _ReturnBindings(
            managed=(
                _new_return_binding(
                    session,
                    logical_key="hush_shell",
                    source_key="hush_shell",
                    public=False,
                ),
                _new_return_binding(
                    session,
                    logical_key="_1",
                    source_key="_1",
                    public=False,
                ),
                _new_return_binding(
                    session,
                    logical_key=session.env["rambase"],
                    source_key=session.env["rambase"],
                    public=False,
                    clear_source=False,
                ),
            )
        )
        script = self._append_continue(
            _preflight_probe_script(session.env["rambase"]),
            session,
            recv_status=None,
            returns=preflight_returns,
        )
        self._log_session_script(session, request, _ensure_newline(script))
        return ContentResult.from_bytes(self.compiler.compile(_ensure_newline(script)))

    def _result_from_instruction(
        self,
        session: ClientSession,
        request: ContentRequest,
        instruction: _ExecutionRequest,
    ) -> ContentResult:
        if not isinstance(instruction, _ExecutionRequest):
            raise TypeError("session handlers must await tftp.exec(...) helpers")
        script = instruction.script.rstrip()
        returns = instruction.returns
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
            script, returns = self._append_receive(
                script,
                session,
                returns,
                instruction.receive_offset,
                instruction.required_cmds,
            )
        elif instruction.final:
            session.phase = "complete"
            session.pending_exec_result_default = None
            script, returns = _apply_required_command_guards(
                session,
                script,
                instruction.required_cmds,
                returns,
            )
            self._discard_session(session)
        else:
            session.current_token = _new_token()
            session.phase = "await_rrq"
            failure_status_script = None
            session.pending_exec_result_default = True
            if instruction.required_cmds and not instruction.final:
                exec_status = _new_return_binding(
                    session,
                    logical_key=_EXEC_STATUS_LOGICAL_KEY,
                    source_key="_s",
                    public=False,
                    kind="exec_status",
                )
                script = _join_script_lines((script, f"setenv {exec_status.capture()} 1"))
                returns = returns.with_managed(exec_status)
                failure_status_script = f"setenv {exec_status.capture()} 0"
                session.pending_exec_result_default = None
            script, returns = _apply_required_command_guards(
                session,
                script,
                instruction.required_cmds,
                returns,
                on_failure_script=failure_status_script,
            )
            script = self._append_continue(
                script,
                session,
                recv_status=None,
                returns=returns,
            )
        self._log_session_script(session, request, _ensure_newline(script))
        return ContentResult.from_bytes(self.compiler.compile(_ensure_newline(script)))

    def _append_continue(
        self,
        script: str,
        session: ClientSession,
        *,
        recv_status: str | None,
        returns: _ReturnBindings = _ReturnBindings(),
    ) -> str:
        command = self._continue_command(
            session,
            recv_status=recv_status,
            returns=returns,
        )
        return _join_script_lines((script, command))

    def _continue_command(
        self,
        session: ClientSession,
        *,
        recv_status: str | None,
        returns: _ReturnBindings = _ReturnBindings(),
    ) -> str:
        if session.current_token is None:
            raise RuntimeError("missing continuation token")
        path = f"id={session.client_id}/token={session.current_token}"
        if recv_status is not None:
            path = f"{path}/recv={recv_status}"
        session.pending_user_return_keys = set(returns.keys)
        session.pending_framework_returns = _assign_framework_returns(returns)
        path = _append_return_keys(path, returns.keys, session.pending_framework_returns)
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
        returns: _ReturnBindings,
        receive_offset: int | str | None,
        required_cmds: tuple[str, ...],
    ) -> tuple[str, _ReturnBindings]:
        pending = session.pending_receive
        if pending is None:
            raise RuntimeError("missing pending receive state")
        upload_address, prelude, cleanup = _receive_address(session, receive_offset)
        missing, probe_commands = _plan_required_commands(session, required_cmds)
        if missing:
            failure = self._continue_command(
                session=session,
                recv_status="failed",
                returns=returns,
            )
            guarded_body = _join_script_lines((*_required_command_error_lines(missing), failure))
            return _join_script_lines((prelude, guarded_body, cleanup)), returns

        if probe_commands:
            probe_returns = tuple(
                _new_return_binding(
                    session,
                    logical_key=None,
                    source_key=f"_c{index}",
                    public=False,
                    kind="probe",
                    command=command,
                )
                for index, command in enumerate(probe_commands)
            )
            probe_lines, probe_keys, key_map = build_probe_batch(
                probe_commands,
                session.env,
                keys=[binding.capture() for binding in probe_returns],
            )
            returns = returns.with_managed(*probe_returns)
            ok_var = _new_tmp_name("cmds_ok")
            checks = [f"setenv {ok_var} 1"]
            for key in probe_keys:
                checks.append(f"if test ${{{key}}} -ne 0; then setenv {ok_var} 0; fi")
        else:
            probe_lines = []
            probe_keys = []
            checks = []
            ok_var = None

        upload_remote = f"id={session.client_id}/token={pending.token}{pending.upload_path}"
        success = self._continue_command(
            session=session,
            recv_status="ok",
            returns=returns,
        )
        failure = self._continue_command(
            session=session,
            recv_status="failed",
            returns=returns,
        )
        receive = (
            f'if {session.env["cmdtftpput"]} {upload_address} {_format_uboot_number(pending.size)} '
            f'"{session.server_ip}:{upload_remote}";\n'
            f"then\n"
            f"    {success}\n"
            f"else\n"
            f"    {failure}\n"
            f"fi"
        )
        if probe_commands:
            failure_lines = _probe_failure_lines(probe_keys, key_map)
            guarded_body = _join_script_lines(
                (
                    *probe_lines,
                    *checks,
                    f"if test ${{{ok_var}}} -eq 1; then",
                    _indent_script(_join_script_lines((script, receive))),
                    "else",
                    _indent_script(_join_script_lines((*failure_lines, failure))),
                    "fi",
                    f"setenv {ok_var}",
                )
            )
        else:
            guarded_body = _join_script_lines((script, receive))
        return _join_script_lines((prelude, guarded_body, cleanup)), returns

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

    def _start_session_log(self, session: ClientSession) -> None:
        if self.session_log_dir is None:
            session.log_path = None
            return
        session.log_path = self.session_log_dir / f"{session.client_id}.log"
        session.log_path.write_text("", encoding="utf-8")

    def _log_session_script(
        self,
        session: ClientSession,
        request: ContentRequest,
        script: str,
    ) -> None:
        if session.log_path is None:
            return
        payload = _format_session_log_entry(request, script)
        with session.log_path.open("a", encoding="utf-8") as handle:
            handle.write(payload)

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
            uboot_msg("Executing preflight... ", nl=False, bold=True),
            "if true; then setenv hush_shell true; fi",
            "setexpr.l t0 *${" + rambase_var + "}",
            f"mw.l {rambase} 0x11223344 1",
            f"setexpr.b _1 *{rambase}",
            f"mw.l {rambase} ${{t0}} 1",
            "setenv t0",
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
    consumed = {"token", "recv"}
    for key in session.pending_user_return_keys:
        value = parsed.values.get(key)
        if value is None:
            continue
        session.env[key] = value
        if key not in {"rambase", "cmdtftp", "cmdtftpput"}:
            session.public_env[key] = value
        consumed.add(key)
    session.pending_user_return_keys.clear()
    consumed.update(_consume_pending_framework_returns(session, parsed))
    for key, value in parsed.values.items():
        if key in consumed:
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


def _normalize_required_cmds(cmds: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(cmd) for cmd in cmds if str(cmd).strip())


def _append_return_keys(
    path: str,
    keys: tuple[str, ...],
    framework_returns: dict[str, PendingFrameworkReturn],
) -> str:
    for key in keys:
        path = f"{path}/{key}=${{{key}}}"
    for wire_key, binding in framework_returns.items():
        path = f"{path}/{wire_key}=${{{binding.source_key}}}"
    return path


def _consume_pending_framework_returns(session: ClientSession, parsed: ParsedPath) -> set[str]:
    if not session.pending_framework_returns:
        return set()
    consumed: set[str] = set()
    cleanup_vars: list[str] = []
    for wire_key, binding in tuple(session.pending_framework_returns.items()):
        value = parsed.values.get(wire_key)
        if value is None:
            continue
        consumed.add(wire_key)
        if binding.logical_key is not None:
            session.env[binding.logical_key] = value
            if binding.public:
                session.public_env[binding.logical_key] = value
        if binding.kind == "probe" and binding.command is not None:
            if value == "0":
                session.unsupported_cmds.discard(binding.command)
                session.supported_cmds.add(binding.command)
            else:
                session.supported_cmds.discard(binding.command)
                session.unsupported_cmds.add(binding.command)
        if binding.clear_source:
            cleanup_vars.append(binding.source_key)
    session.pending_framework_returns.clear()
    for name in cleanup_vars:
        if name not in session.pending_cleanup_vars:
            session.pending_cleanup_vars.append(name)
    return consumed


def _consume_exec_status(session: ClientSession) -> object:
    if session.pending_exec_result_default is not None:
        value = session.pending_exec_result_default
        session.pending_exec_result_default = None
        return value
    status = session.env.pop(_EXEC_STATUS_LOGICAL_KEY, None)
    session.public_env.pop(_EXEC_STATUS_LOGICAL_KEY, None)
    if status is None:
        return _NO_EXEC_RESULT
    return status == "1"


def _apply_required_command_guards(
    session: ClientSession,
    script: str,
    required_cmds: tuple[str, ...],
    returns: _ReturnBindings,
    *,
    failure_script: str | None = None,
    on_failure_script: str | None = None,
) -> tuple[str, _ReturnBindings]:
    if not required_cmds:
        return script, returns

    missing, probe_commands = _plan_required_commands(session, required_cmds)

    if missing:
        return _join_script_lines(
            (on_failure_script, *_required_command_error_lines(missing), failure_script)
        ), returns

    if not probe_commands:
        return script, returns

    probe_returns = tuple(
        _new_return_binding(
            session,
            logical_key=None,
            source_key=f"_c{index}",
            public=False,
            kind="probe",
            command=command,
        )
        for index, command in enumerate(probe_commands)
    )
    probe_lines, probe_keys, key_map = build_probe_batch(
        probe_commands,
        session.env,
        keys=[binding.capture() for binding in probe_returns],
    )
    returns = returns.with_managed(*probe_returns)
    ok_var = _new_tmp_name("cmds_ok")
    checks = [f"setenv {ok_var} 1"]
    for key in probe_keys:
        checks.append(f"if test ${{{key}}} -ne 0; then setenv {ok_var} 0; fi")
    failure_lines = _probe_failure_lines(probe_keys, key_map)
    wrapped = _join_script_lines(
        (
            *probe_lines,
            *checks,
            f"if test ${{{ok_var}}} -eq 1; then",
            _indent_script(script),
            "else",
            _indent_script(_join_script_lines((on_failure_script, *failure_lines, failure_script))),
            "fi",
            f"setenv {ok_var}",
        )
    )
    return wrapped, returns


def _plan_required_commands(
    session: ClientSession,
    required_cmds: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    commands = normalize_requested_commands(list(required_cmds), session.env)
    session_proven = set(normalize_requested_commands(["cmdtftp"], session.env))
    missing: list[str] = []
    probe_commands: list[str] = []
    for command in commands:
        if command in session_proven:
            session.unsupported_cmds.discard(command)
            session.supported_cmds.add(command)
            continue
        if command in session.supported_cmds:
            continue
        if command in session.unsupported_cmds:
            missing.append(command)
            continue
        try:
            spec = get_command_spec(command)
        except ValueError:
            session.supported_cmds.discard(command)
            session.unsupported_cmds.add(command)
            missing.append(command)
            continue
        if spec.policy == "assumed":
            session.unsupported_cmds.discard(command)
            session.supported_cmds.add(command)
            continue
        if spec.policy == "probe":
            probe_commands.append(command)
            continue
        raise ValueError(f"unexpected command policy for {command!r}: {spec.policy}")
    return missing, probe_commands


def _required_command_error_lines(commands: list[str]) -> list[str]:
    return [uboot_err(f"uboot-tftp: required commands unavailable: {', '.join(commands)}")]


def _probe_failure_lines(probe_keys: list[str], key_map: dict[str, str]) -> list[str]:
    reverse_map = {key: command for command, key in key_map.items()}
    lines: list[str] = []
    for key in probe_keys:
        command = reverse_map[key]
        lines.append(f"if test ${{{key}}} -ne 0; then")
        lines.append(f"    {uboot_err(f'uboot-tftp: required command unavailable: {command}')}")
        lines.append("fi")
    return lines


def _indent_script(script: str | None) -> str:
    if not script:
        return ""
    return "\n".join(f"    {line}" if line else "" for line in script.splitlines())


_NO_EXEC_RESULT = object()
_EXEC_STATUS_LOGICAL_KEY = "__exec_status"
_SCRIPT_TMP_COUNTER = itertools.count(0)


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


def _reset_per_instruction_allocators(session: ClientSession) -> None:
    global _SCRIPT_TMP_COUNTER
    _SCRIPT_TMP_COUNTER = itertools.count(0)
    session.framework_return_index = 0
    reset_script_snippet_tmp_counter()


def _new_tmp_name(kind: str) -> str:
    _ = kind
    return f"t{next(_SCRIPT_TMP_COUNTER)}"


def _new_return_binding(
    session: ClientSession,
    logical_key: str | None = None,
    *,
    source_key: str | None = None,
    public: bool = False,
    clear_source: bool = True,
    kind: str = "value",
    command: str | None = None,
) -> ReturnBinding:
    if source_key is None:
        source_key = f"_r{session.framework_return_index}"
        session.framework_return_index += 1
    return ReturnBinding(
        session=session,
        source_key=source_key,
        logical_key=logical_key,
        public=public,
        clear_source=clear_source,
        kind=kind,
        command=command,
    )


def _assign_framework_returns(
    returns: _ReturnBindings,
) -> dict[str, PendingFrameworkReturn]:
    reserved = set(returns.keys)
    assigned: dict[str, PendingFrameworkReturn] = {}
    index = 0
    for binding in returns.managed:
        while True:
            wire_key = f"_{index}"
            index += 1
            if wire_key not in reserved:
                break
        assigned[wire_key] = PendingFrameworkReturn(
            wire_key=wire_key,
            source_key=binding.source_key,
            logical_key=binding.logical_key,
            public=binding.public,
            clear_source=binding.clear_source,
            kind=binding.kind,  # type: ignore[arg-type]
            command=binding.command,
        )
    return assigned


def _queue_pending_cleanup(session: ClientSession) -> None:
    if not session.pending_cleanup_vars:
        return
    cleanup = [f"setenv {name}" for name in session.pending_cleanup_vars]
    session.pending_cleanup_vars.clear()
    session.queued_scripts = [*cleanup, *session.queued_scripts]


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


def _format_session_log_entry(request: ContentRequest, script: str) -> str:
    request_lines = [
        "REQUEST",
        f"filename: {request.filename}",
        f"peer: {request.peer}",
        f"server_addr: {request.server_addr}",
        f"options: {json.dumps(dict(request.options), sort_keys=True)}",
        "SCRIPT",
        _sanitize_logged_script(script),
        "",
    ]
    return "\n".join(request_lines) + "\n"


_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|.)")
_CLEAR_SEQUENCE_RE = re.compile(r"(?:\x1b8)?(?:\x1b\[2J|\x1b\[J)(?:\x1b\[0m)?(?:\x1b\[H)?(?:\x1b7)?")
_ECHO_SEGMENT_RE = re.compile(r'^echo "((?:[^"\\]|\\.)*)"$')


def _sanitize_logged_script(script: str) -> str:
    lines: list[str] = []
    for line in script.rstrip("\n").splitlines():
        cleaned = _sanitize_logged_line(line)
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def _sanitize_logged_line(line: str) -> str:
    segments = [segment.strip() for segment in _split_shell_segments(line)]
    cleaned_segments: list[str] = []
    for segment in segments:
        if not segment:
            continue
        cleaned_segment = _sanitize_echo_segment(segment)
        if cleaned_segment:
            cleaned_segments.append(cleaned_segment)
    return "; ".join(cleaned_segments)


def _sanitize_echo_segment(segment: str) -> str:
    echo_match = _ECHO_SEGMENT_RE.fullmatch(segment)
    if echo_match is not None:
        message = _sanitize_echo_payload(echo_match.group(1))
        return f'echo "{message}"' if message else ""
    if not segment.startswith("echo "):
        return segment
    message = _sanitize_echo_payload(segment[5:])
    return f'echo "{message}"' if message else ""


def _sanitize_echo_payload(payload: str) -> str:
    message = payload.replace('\\"', '"').replace("\\c", "")
    message = _CLEAR_SEQUENCE_RE.sub("<clear>", message)
    message = _ANSI_ESCAPE_RE.sub("", message)
    message = "".join(character for character in message if character.isprintable())
    message = re.sub(r"(?:<clear>)+", "<clear>", message)
    return message.strip()


def _split_shell_segments(line: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    in_quotes = False
    escaped = False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\":
            current.append(character)
            escaped = True
            continue
        if character == '"':
            current.append(character)
            in_quotes = not in_quotes
            continue
        if character == ";" and not in_quotes:
            segments.append("".join(current))
            current = []
            continue
        current.append(character)
    segments.append("".join(current))
    return segments


class _ExecutionAwaitable:
    def __init__(self, request: _ExecutionRequest) -> None:
        self.request = request

    def __await__(self):
        result = yield self.request
        return result
