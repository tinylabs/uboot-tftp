import threading
import pytest
import re

from uboot_tftp.config import load_daemon_config
from uboot_tftp.download_jobs import DownloadJobStore
from uboot_tftp.mkimage import extract_script_payload
from uboot_tftp.providers import ContentRequest
from uboot_tftp.scripted import ScriptedSessionProvider
from uboot_tftp.sessions import InMemorySessionStore
from uboot_tftp.uploads import InMemoryUploadStore, UploadRequest


def script_from_result(result):
    return extract_script_payload(result.body).decode("utf-8")


TOKEN_RE = re.compile(r'id=[^/]+/token=([^"/]+)')


def request(filename):
    return ContentRequest(
        filename=filename,
        peer=("127.0.0.1", 12345),
        server_addr=("127.0.0.1", 6969),
        options={"mode": "octet"},
    )


def preflight_session(provider, filename):
    script = script_from_result(provider.fetch(request(filename)))
    token_match = TOKEN_RE.search(script)
    assert token_match is not None
    client_id = filename.split("/", 1)[0].removeprefix("id=")
    return client_id, script, token_match.group(1)


def start_session_script(provider, filename):
    client_id, _, token = preflight_session(provider, filename)
    return script_from_result(
        provider.fetch(
            request(f"id={client_id}/token={token}/hush_shell=true/_1=44/loadaddr=0x42000000")
        )
    )


def write_config(tmp_path, script_body, route="handler"):
    script = tmp_path / "script.py"
    script.write_text(script_body)
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            (
                "[server]",
                'scriptfile = "script.py"',
                f'rootdir = "{(tmp_path / "static").resolve()}"',
                "",
                "[env]",
                'rambase = "loadaddr"',
                'cmdtftp = "tftpboot"',
                'cmdtftpput = "tftpput"',
                "",
                "[cam123]",
                f'entry_func = "{route}"',
                "",
                "[default]",
                'entry_func = "default"',
            )
        )
    )
    return load_daemon_config(config)


def test_scripted_provider_routes_by_client_id_and_passes_path(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec([f'echo known {ident} {cmd} {env.get(\"board\", \"-\")}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec([f'echo default {ident} {cmd}'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    assert "echo known cam123 boot -" in start_session_script(provider, "id=cam123/boot")
    assert "echo default other123 boot" in start_session_script(provider, "id=other123/boot")


def test_scripted_provider_serves_static_file_for_bare_rrq(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo known'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    static_root = tmp_path / "static"
    static_root.mkdir()
    (static_root / "uImage").write_bytes(b"bare-static-image")

    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    result = provider.fetch(request("uImage"))
    assert result.body == b"bare-static-image"


def test_session_handle_exposes_absolute_static_root(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec([f'echo root {tftp.root}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    script = start_session_script(provider, "id=cam123/boot")

    assert f"echo root {tmp_path / 'static'}" in script


def test_session_handle_exposes_resolved_rambase_address(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec([f'echo rambase {hex(tftp.rambase_addr)}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    script = start_session_script(provider, "id=cam123/boot")

    assert "echo rambase 0x42000000" in script


def test_session_handle_rambase_addr_requires_resolved_env_value(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    try:",
                "        _ = tftp.rambase_addr",
                "    except RuntimeError as exc:",
                "        await tftp.exec([f'echo missing {exc}'], final=True)",
                "        return",
                "    await tftp.exec(['echo unexpected'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    client_id, _, token = preflight_session(provider, "id=cam123/boot")
    script = script_from_result(
        provider.fetch(request(f"id={client_id}/token={token}/hush_shell=true/_1=44"))
    )

    assert "resolved RAM base value is missing for 'loadaddr'" in script


def test_session_handle_rambase_addr_rejects_invalid_env_value(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    try:",
                "        _ = tftp.rambase_addr",
                "    except ValueError as exc:",
                "        await tftp.exec([f'echo invalid {exc}'], final=True)",
                "        return",
                "    await tftp.exec(['echo unexpected'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    client_id, _, token = preflight_session(provider, "id=cam123/boot/loadaddr=bogus")
    script = script_from_result(
        provider.fetch(request(f"id={client_id}/token={token}/hush_shell=true/_1=44/loadaddr=bogus"))
    )

    assert "invalid RAM base value for 'loadaddr': 'bogus'" in script


def test_initial_session_runs_hush_preflight_before_user_handler(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo handler ran'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    client_id, preflight, token = preflight_session(provider, "id=cam123/bootstrap")

    assert client_id == "cam123"
    assert "Executing preflight..." in preflight
    assert 'if true; then setenv hush_shell true; fi' in preflight
    assert 'setexpr.b _1 *${loadaddr}' in preflight
    assert f'/_0=${{hush_shell}}/_1=${{_1}}/_2=${{loadaddr}}"' in preflight
    assert "echo handler ran" not in preflight

    second = script_from_result(
        provider.fetch(
            request(f"id={client_id}/token={token}/hush_shell=true/_1=44/loadaddr=0x42000000")
        )
    )
    assert "echo handler ran" in second


def test_initial_session_fails_when_hush_shell_is_unavailable(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo handler ran'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    client_id, _, token = preflight_session(provider, "id=cam123/bootstrap")
    failure = script_from_result(provider.fetch(request(f"id={client_id}/token={token}")))

    assert "U-Boot hush shell is required" in failure
    assert "hush-compatible if/then support" in failure
    assert "echo handler ran" not in failure
    assert sessions.get("cam123") is None


def test_session_handle_exposes_preflight_endianness(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec([f'echo endian {tftp.is_le}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    client_id, _, token = preflight_session(provider, "id=cam123/bootstrap")
    second = script_from_result(
        provider.fetch(
            request(f"id={client_id}/token={token}/hush_shell=true/_1=44/loadaddr=0x42000000")
        )
    )

    assert "echo endian True" in second


def test_exec_appends_internal_continuation_rrq(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo step1'])",
                "    await tftp.exec(['echo step2'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    assert "echo step1" in first
    first_token = TOKEN_RE.search(first)
    assert first_token is not None
    first_token = first_token.group(1)
    assert f'tftpboot ${{loadaddr}} "127.0.0.1:id=cam123/token={first_token}"' in first

    second = script_from_result(provider.fetch(request(f"id=cam123/token={first_token}")))
    assert "echo step2" in second
    assert "token=" not in second


def test_exec_can_request_return_keys_for_next_rrq(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo export env'], keys=['filesize'])",
                "    await tftp.exec([f'echo filesize {env[\"filesize\"]}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert '/filesize=${filesize}"' in first

    second = script_from_result(
        provider.fetch(request(f"id=cam123/token={token}/filesize=1235"))
    )
    assert "echo filesize 1235" in second


def test_exec_managed_returns_use_short_keys_and_support_typed_access(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    crc = tftp.bind('crc')",
                "    await tftp.exec([f'setenv {crc.capture()} 0x2a'], returns=[crc])",
                "    await tftp.exec([f'echo crc {crc.str()} {crc.int()} {tftp.env[\"crc\"]}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert "setenv _r0 0x2a" in first
    assert '/_0=${_r0}"' in first

    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}/_0=0x2a")))
    assert "setenv _r0" in second
    assert "echo crc 0x2a 42 0x2a" in second


def test_exec_can_mix_literal_keys_with_managed_returns(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    crc = tftp.bind('crc')",
                "    await tftp.exec([f'setenv {crc.capture()} 0x10'], keys=['literal'], returns=[crc])",
                "    await tftp.exec([f'echo values {env[\"literal\"]} {crc.int()}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert '/literal=${literal}/_0=${_r0}"' in first

    second = script_from_result(
        provider.fetch(request(f"id=cam123/token={token}/literal=ok/_0=0x10"))
    )
    assert "echo values ok 16" in second


def test_exec_returns_true_without_requires(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    ok = await tftp.exec(['echo step1'])",
                "    await tftp.exec([f'echo ok {ok}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert "echo step1" in first

    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}/_0=0")))
    assert "echo ok True" in second


def test_exec_queue_prepends_once_and_clears_on_exec(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    tftp.exec_queue(['echo queued1', 'echo queued2'])",
                "    await tftp.exec(['echo body'])",
                "    await tftp.exec(['echo next'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    assert first.index("echo queued1") < first.index("echo queued2") < first.index("echo body")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)

    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}/_0=0")))
    assert "echo queued1" not in second
    assert "echo queued2" not in second
    assert "echo next" in second


def test_exec_queue_merges_requires_into_next_exec(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    tftp.exec_queue(['echo queued'], requires=['bootflow scan'])",
                "    ok = await tftp.exec(['echo body'])",
                "    await tftp.exec([f'echo checked {ok}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    assert "echo queued" not in first
    assert "echo body" not in first
    assert "required commands unavailable: bootflow scan" in first
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)

    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}/_0=0")))
    assert "echo checked False" in second


def test_exec_queue_is_consumed_by_exec_recv(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "from uboot_tftp.scripted import ReceiveFailedError",
                "",
                "async def handler(tftp, ident, cmd, env):",
                "    tftp.exec_queue(['echo queued'])",
                "    try:",
                "        await tftp.exec_recv(['echo upload'], 8)",
                "    except ReceiveFailedError:",
                "        await tftp.exec(['echo fallback'], final=True)",
                "        return",
                "    await tftp.exec(['echo done'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    uploads = InMemoryUploadStore(sessions)
    provider = ScriptedSessionProvider(config, sessions=sessions, upload_store=uploads)

    first = start_session_script(provider, "id=cam123/bootstrap")
    assert first.index("echo queued") < first.index("echo upload")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)

    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}/recv=failed")))
    assert "echo queued" not in second
    assert "echo fallback" in second


def test_exec_queue_merges_requires_into_next_exec_recv(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "from uboot_tftp.scripted import ReceiveFailedError",
                "",
                "async def handler(tftp, ident, cmd, env):",
                "    tftp.exec_queue(['echo queued'], requires=['bootflow scan'])",
                "    try:",
                "        await tftp.exec_recv(['echo upload'], 8)",
                "    except ReceiveFailedError:",
                "        await tftp.exec(['echo fallback'], final=True)",
                "        return",
                "    await tftp.exec(['echo unexpected'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    uploads = InMemoryUploadStore(sessions)
    provider = ScriptedSessionProvider(config, sessions=sessions, upload_store=uploads)

    first = start_session_script(provider, "id=cam123/bootstrap")
    assert "echo queued" not in first
    assert "echo upload" not in first
    assert "required commands unavailable: bootflow scan" in first
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)

    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}/recv=failed")))
    assert "echo fallback" in second
    assert "echo unexpected" not in second


def test_target_route_overrides_transport_env_for_new_session(tmp_path):
    script = tmp_path / "script.py"
    script.write_text(
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec([f'echo route override {env[\"extra\"]}'])",
                "    await tftp.exec_recv(['echo receive'], 8)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        )
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            (
                "[server]",
                'scriptfile = "script.py"',
                f'rootdir = "{(tmp_path / "static").resolve()}"',
                "",
                "[env]",
                'rambase = "baseaddr"',
                'cmdtftp = "tftpboot"',
                'cmdtftpput = "tftpput"',
                "",
                "[cam123]",
                'entry_func = "handler"',
                'rambase = "loadaddr"',
                'cmdtftp = "dhcp"',
                'cmdtftpput = "nmrp"',
                'extra = "route"',
                "",
                "[default]",
                'entry_func = "default"',
            )
        )
    )
    config = load_daemon_config(config_path)
    sessions = InMemorySessionStore()
    uploads = InMemoryUploadStore(sessions)
    provider = ScriptedSessionProvider(config, sessions=sessions, upload_store=uploads)

    first = start_session_script(provider, "id=cam123/bootstrap/extra=rrq")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert "echo route override rrq" in first
    assert f'dhcp ${{loadaddr}} "127.0.0.1:id=cam123/token={token}"' in first

    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}")))
    next_token_match = TOKEN_RE.search(second)
    assert next_token_match is not None
    next_token = next_token_match.group(1)
    assert f'nmrp ${{loadaddr}} 0x8 "127.0.0.1:id=cam123/token={next_token}/upload.bin"' in second


def test_exec_recv_returns_uploaded_bytes_on_followup_rrq(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    data = await tftp.exec_recv(['echo send upload'], 8, keys=['filesize'])",
                "    parsed = tftp.parse_env_export(data)",
                "    tftp.write_file('saved/dump.bin', data)",
                "    await tftp.exec([f'echo done {parsed[\"ethaddr\"]} {env[\"filesize\"]}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    uploads = InMemoryUploadStore(sessions)
    provider = ScriptedSessionProvider(config, sessions=sessions, upload_store=uploads)

    first = start_session_script(provider, "id=cam123/bootstrap")
    assert "echo send upload" in first
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert f'tftpput ${{loadaddr}} 0x8 "127.0.0.1:id=cam123/token={token}/upload.bin"' in first
    assert (
        f'tftpboot ${{loadaddr}} '
        f'"127.0.0.1:id=cam123/token={token}/recv=ok/filesize=${{filesize}}"' in first
    )

    upload = uploads.open(
        UploadRequest(
            filename=f"id=cam123/token={token}/upload.bin",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
        )
    )
    upload.write(b"ethaddr=00:11:22:33:44:55\x00")
    upload.close()

    second = script_from_result(
        provider.fetch(request(f"id=cam123/token={token}/recv=ok/filesize=1235"))
    )
    assert "echo done 00:11:22:33:44:55 1235" in second
    assert (tmp_path / "static" / "saved" / "dump.bin").read_bytes() == (
        b"ethaddr=00:11:22:33:44:55\x00"
    )


def test_file_exists_checks_relative_path_under_tftp_root(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    tftp.write_file('saved/dump.bin', b'payload')",
                "    missing = 'ok'",
                "    unsafe = 'ok'",
                "    try:",
                "        tftp.read_file('missing.bin')",
                "    except FileNotFoundError:",
                "        missing = 'missing'",
                "    try:",
                "        tftp.read_file('../outside.bin')",
                "    except ValueError:",
                "        unsafe = 'unsafe'",
                "    await tftp.exec([",
                "        f'echo exists {tftp.file_exists(\"saved/dump.bin\")}',",
                "        f'echo read {tftp.read_file(\"saved/dump.bin\").decode()}',",
                "        f'echo missing_read {missing}',",
                "        f'echo missing {tftp.file_exists(\"missing.bin\")}',",
                "        f'echo unsafe {unsafe}',",
                "    ], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    script = start_session_script(provider, "id=cam123/bootstrap")

    assert "echo exists True" in script
    assert "echo read payload" in script
    assert "echo missing_read missing" in script
    assert "echo missing False" in script
    assert "echo unsafe unsafe" in script
    assert (tmp_path / "static" / "saved" / "dump.bin").read_bytes() == b"payload"


def test_exec_recv_can_upload_from_relative_rambase_offset(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec_recv(['echo send upload'], 8, offset=0x400)",
                "    await tftp.exec(['echo done'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert "setexpr t0 ${loadaddr} + 0x400" in first
    recv_tmp = "t0"
    assert (
        f'tftpput ${{{recv_tmp}}} 0x8 "127.0.0.1:id=cam123/token={token}/upload.bin"' in first
    )
    assert f"setenv {recv_tmp}" in first


def test_exec_recv_can_be_caught_by_user_script(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "from uboot_tftp.scripted import ReceiveFailedError",
                "",
                "async def handler(tftp, ident, cmd, env):",
                "    try:",
                "        await tftp.exec_recv(['echo send upload'], 8)",
                "    except ReceiveFailedError:",
                "        await tftp.exec(['echo recv failed'], final=True)",
                "        return",
                "    await tftp.exec(['echo unexpected'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    uploads = InMemoryUploadStore(sessions)
    provider = ScriptedSessionProvider(config, sessions=sessions, upload_store=uploads)

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}/recv=failed")))
    assert "echo recv failed" in second


def test_exec_recv_rejects_final_true(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec_recv(['echo bad'], 8, final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    client_id, _, token = preflight_session(provider, "id=cam123/bootstrap")
    with pytest.raises(ValueError, match="final=True"):
        provider.fetch(request(f"id={client_id}/token={token}/hush_shell=true"))


def test_initial_rrq_values_override_route_and_base_env(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec([",
                "        f'echo cmd {cmd}',",
                "        f'echo host {env[\"host\"]}',",
                "        f'echo mode {env[\"mode\"]}',",
                "    ], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    config.env["host"] = "base"
    config.routes["cam123"].env["host"] = "route"
    config.routes["cam123"].env["mode"] = "route-mode"
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    script = start_session_script(provider, "id=cam123/bootstrap/host=rrq/mode=rrq-mode")
    assert "echo cmd bootstrap" in script
    assert "echo host rrq" in script
    assert "echo mode rrq-mode" in script


def test_transport_keys_are_removed_from_env_argument(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec([",
                "        f'echo has_rambase {\"rambase\" in env}',",
                "        f'echo has_cmdtftp {\"cmdtftp\" in env}',",
                "        f'echo has_cmdtftpput {\"cmdtftpput\" in env}',",
                "        f'echo user_value {env[\"user\"]}',",
                "    ], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    config.env["user"] = "visible"
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    script = start_session_script(provider, "id=cam123/bootstrap")
    assert "echo has_rambase False" in script
    assert "echo has_cmdtftp False" in script
    assert "echo has_cmdtftpput False" in script
    assert "echo user_value visible" in script


def test_fetch_env_helper_exports_receives_and_parses_environment(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    exported = await tftp.fetch_env()",
                "    await tftp.exec([f'echo ethaddr {exported[\"ethaddr\"]} {env[\"filesize\"]}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    uploads = InMemoryUploadStore(sessions)
    provider = ScriptedSessionProvider(config, sessions=sessions, upload_store=uploads)

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert f"env export -t ${{loadaddr}}" in first
    assert f'/_0=${{filesize}}"' in first

    second = script_from_result(
        provider.fetch(request(f"id=cam123/token={token}/filesize=1235"))
    )
    second_token_match = TOKEN_RE.search(second)
    assert second_token_match is not None
    second_token = second_token_match.group(1)
    assert (
        f'tftpput ${{loadaddr}} 0x1235 "127.0.0.1:id=cam123/token={second_token}/upload.bin"'
        in second
    )

    upload = uploads.open(
        UploadRequest(
            filename=f"id=cam123/token={second_token}/upload.bin",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
        )
    )
    upload.write(b"ethaddr=00:11:22:33:44:55\x00")
    upload.close()

    third = script_from_result(
        provider.fetch(request(f"id=cam123/token={second_token}/recv=ok"))
    )
    assert "echo ethaddr 00:11:22:33:44:55 1235" in third


def test_session_handle_can_acquire_and_poll_shared_download_artifact(tmp_path):
    events: list[str] = []
    release = threading.Event()

    def downloader(request, progress):
        events.append(request.artifact_key)
        release.wait(timeout=1)
        request.temp_path.write_bytes(b"fw")
        progress(2, 2)

    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    artifact = tftp.acquire_download(",
                "        artifact_key='shared-fw',",
                "        url='https://example/fw.bin',",
                "        destination='cache/fw.bin',",
                "    )",
                "    polled = tftp.get_download('shared-fw')",
                "    await tftp.exec([",
                "        f'echo state {artifact.state}',",
                "        f'echo path {polled.relative_path}',",
                "    ], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    downloads = DownloadJobStore(temp_root=tmp_path / "downloads", downloader=downloader)
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config,
        sessions=sessions,
        upload_store=InMemoryUploadStore(sessions),
        download_jobs=downloads,
    )

    script = start_session_script(provider, "id=cam123/bootstrap")

    assert "echo state pending" in script
    assert "echo path cache/fw.bin" in script
    for _ in range(20):
        if events:
            break
        threading.Event().wait(0.01)
    assert events == ["shared-fw"]
    release.set()


def test_session_cleanup_releases_attached_download_artifacts(tmp_path):
    release = threading.Event()

    def downloader(request, progress):
        release.wait(timeout=1)
        request.temp_path.write_bytes(b"fw")
        progress(2, 2)

    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    tftp.acquire_download(",
                "        artifact_key='shared-fw',",
                "        url='https://example/fw.bin',",
                "        destination='cache/fw.bin',",
                "    )",
                "    await tftp.exec(['echo done'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    downloads = DownloadJobStore(temp_root=tmp_path / "downloads", downloader=downloader)
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config,
        sessions=sessions,
        upload_store=InMemoryUploadStore(sessions),
        download_jobs=downloads,
    )

    start_session_script(provider, "id=cam123/bootstrap")
    artifact = downloads.get("shared-fw")
    assert artifact is not None
    assert artifact.ref_count == 0
    release.set()


def test_check_cmds_returns_assumed_commands_without_issuing_probe(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    supported = await tftp.check_cmds(['source'])",
                "    await tftp.exec([f'echo supported {\"|\".join(supported)}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    script = start_session_script(provider, "id=cam123/bootstrap")

    assert "/_0=" not in script
    assert "echo supported source|true|if|echo|tftpboot" in script


def test_check_cmds_resolves_transport_aliases_via_session_config(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    supported = await tftp.check_cmds(['cmdtftp', 'cmdtftpput'])",
                "    await tftp.exec([f'echo supported {\"|\".join(supported)}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    config.env["cmdtftp"] = "dhcp"
    config.env["cmdtftpput"] = "tftpput"
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert "cmdtftp" not in first
    assert "cmdtftpput" not in first
    assert "dhcp" in first
    assert "tftpput" in first

    second = script_from_result(
        provider.fetch(request(f"id=cam123/token={token}/_0=0/_1=0"))
    )
    assert "echo supported dhcp|tftpput|source|true|if|echo" in second


def test_check_cmds_appends_framework_required_commands(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    supported = await tftp.check_cmds([])",
                "    await tftp.exec([f'echo supported {\"|\".join(supported)}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    script = start_session_script(provider, "id=cam123/bootstrap")
    assert "echo supported source|true|if|echo|tftpboot" in script


def test_check_cmds_only_probes_uncached_commands_within_session(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    first = await tftp.check_cmds(['env export'])",
                "    second = await tftp.check_cmds(['env export', 'sf probe'])",
                "    await tftp.exec([",
                "        f'echo first {\"|\".join(first)}',",
                "        f'echo second {\"|\".join(second)}',",
                "    ], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    first_token_match = TOKEN_RE.search(first)
    assert first_token_match is not None
    first_token = first_token_match.group(1)
    assert "env export -t ${loadaddr}" in first
    assert "sf probe 0" not in first

    second = script_from_result(
        provider.fetch(request(f"id=cam123/token={first_token}/_0=0"))
    )
    second_token_match = TOKEN_RE.search(second)
    assert second_token_match is not None
    second_token = second_token_match.group(1)
    assert "env export -t ${loadaddr}" not in second
    assert "sf probe 0" in second

    third = script_from_result(
        provider.fetch(request(f"id=cam123/token={second_token}/_0=0"))
    )
    assert "echo first env export|source|true|if|echo|tftpboot" in third
    assert "echo second env export|sf probe|source|true|if|echo|tftpboot" in third


def test_check_cmds_keeps_unsupported_probed_commands_omitted_on_later_calls(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    first = await tftp.check_cmds(['sf write'])",
                "    second = await tftp.check_cmds(['sf write'])",
                "    await tftp.exec([",
                "        f'echo first {\"|\".join(first)}',",
                "        f'echo second {\"|\".join(second)}',",
                "    ], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert "sf probe 0" in first

    second = script_from_result(
        provider.fetch(request(f"id=cam123/token={token}/_0=1"))
    )
    assert "sf probe 0" not in second
    assert "echo first source|true|if|echo|tftpboot" in second
    assert "echo second source|true|if|echo|tftpboot" in second


def test_check_cmds_uses_configured_rambase_variable_for_probes(tmp_path):
    script = tmp_path / "script.py"
    script.write_text(
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    supported = await tftp.check_cmds(['sf read'])",
                "    await tftp.exec([f'echo supported {\"|\".join(supported)}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        )
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            (
                "[server]",
                'scriptfile = "script.py"',
                f'rootdir = "{(tmp_path / "static").resolve()}"',
                "",
                "[env]",
                'rambase = "baseaddr"',
                'cmdtftp = "tftpboot"',
                'cmdtftpput = "tftpput"',
                "",
                "[cam123]",
                'entry_func = "handler"',
                "",
                "[default]",
                'entry_func = "default"',
            )
        )
    )
    config = load_daemon_config(config_path)
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    client_id, _, token = preflight_session(provider, "id=cam123/bootstrap/baseaddr=0x42000000")
    script_text = script_from_result(
        provider.fetch(
            request(f"id={client_id}/token={token}/hush_shell=true/_1=44/baseaddr=0x42000000")
        )
    )

    assert "sf read ${baseaddr} 0x0 0x1" in script_text
    assert "${loadaddr}" not in script_text


def test_check_cmds_treats_unknown_commands_as_unsupported(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    supported = await tftp.check_cmds(['bootflow scan', 'source'])",
                "    await tftp.exec([f'echo supported {\"|\".join(supported)}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    script = start_session_script(provider, "id=cam123/bootstrap")
    assert "bootflow scan" not in script
    assert "echo supported source|true|if|echo|tftpboot" in script


def test_exec_requires_treats_unknown_commands_as_unavailable(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo guarded'], requires=['bootflow scan'])",
                "    await tftp.exec(['echo fallback'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    script = start_session_script(provider, "id=cam123/bootstrap")
    assert "echo guarded" not in script
    assert "required commands unavailable: bootflow scan" in script
    assert "echo fallback" not in script

    token_match = TOKEN_RE.search(script)
    assert token_match is not None
    token = token_match.group(1)
    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}")))
    assert "echo fallback" in second


def test_exec_checked_returns_true_when_guarded_body_runs(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    ok = await tftp.exec(['echo guarded'], requires=['source'])",
                "    await tftp.exec([f'echo checked {ok}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert "echo guarded" in first
    assert "/_0=${_s}" in first
    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}/_0=1")))
    assert "echo checked True" in second


def test_exec_checked_returns_false_when_guarded_body_is_skipped(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    ok = await tftp.exec(['echo guarded'], requires=['bootflow scan'])",
                "    await tftp.exec([f'echo checked {ok}'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert "echo guarded" not in first
    assert "/_0=${_s}" in first
    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}/_0=0")))
    assert "echo checked False" in second


def test_exec_requires_final_true_remains_fire_and_forget(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo bad'], requires=['bootflow scan'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    script = start_session_script(provider, "id=cam123/bootstrap")
    assert "echo bad" not in script
    assert "required commands unavailable: bootflow scan" in script


def test_exec_requires_skips_body_and_continues_on_missing_command(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo guarded'], requires=['sf write'])",
                "    await tftp.exec(['echo fallback'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert "sf probe 0" in first
    assert "echo guarded" in first

    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}/_0=0/_1=1")))
    assert "echo fallback" in second


def test_exec_requires_uses_cached_unsupported_result_without_reprobe(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo first'], requires=['sf write'])",
                "    await tftp.exec(['echo second'], requires=['sf write'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)

    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}/_0=0/_1=1")))
    assert "sf write ${loadaddr} 0x0 0x0" not in second
    assert "echo second" not in second
    assert "required commands unavailable: sf write" in second


def test_exec_recv_requires_uses_failure_continuation_for_missing_command(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "from uboot_tftp.scripted import ReceiveFailedError",
                "",
                "async def handler(tftp, ident, cmd, env):",
                "    try:",
                "        await tftp.exec_recv(['echo upload'], 8, requires=['cmdtftpput'])",
                "    except ReceiveFailedError:",
                "        await tftp.exec(['echo fallback'], final=True)",
                "        return",
                "    await tftp.exec(['echo unexpected'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config, sessions=sessions, upload_store=InMemoryUploadStore(sessions)
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert "tftpput ${loadaddr} 0x0 _null" in first
    assert "echo upload" in first
    assert "/recv=failed/_0=${_c0}" in first

    second = script_from_result(provider.fetch(request(f"id=cam123/token={token}/recv=failed/_0=1")))
    assert "echo fallback" in second
    assert "echo unexpected" not in second


def test_session_logging_writes_request_and_script_blocks(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo step1'])",
                "    await tftp.exec(['echo step2'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    log_dir = tmp_path / "logs"
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config,
        sessions=sessions,
        upload_store=InMemoryUploadStore(sessions),
        session_log_dir=log_dir,
    )

    first = start_session_script(provider, "id=cam123/bootstrap")
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    script_from_result(provider.fetch(request(f"id=cam123/token={token}")))

    log_path = log_dir / "cam123.log"
    payload = log_path.read_text()
    assert payload.count("REQUEST\n") == 3
    assert "filename: id=cam123/bootstrap" in payload
    assert "filename: id=cam123/token=" in payload
    assert 'echo "<clear>OK"' in payload
    assert 'echo "<clear>Executing preflight..."' in payload
    assert 'echo "step1"' in payload
    assert 'SCRIPT\necho "step2"' in payload
    assert "\x1b" not in payload


def test_session_logging_overwrites_existing_ident_log_on_new_session(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "async def handler(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo session'], final=True)",
                "",
                "async def default(tftp, ident, cmd, env):",
                "    await tftp.exec(['echo default'], final=True)",
            )
        ),
    )
    log_dir = tmp_path / "logs"
    sessions = InMemorySessionStore()
    provider = ScriptedSessionProvider(
        config,
        sessions=sessions,
        upload_store=InMemoryUploadStore(sessions),
        session_log_dir=log_dir,
    )

    start_session_script(provider, "id=cam123/first")
    log_path = log_dir / "cam123.log"
    initial = log_path.read_text()
    assert "filename: id=cam123/first" in initial

    start_session_script(provider, "id=cam123/second")
    overwritten = log_path.read_text()
    assert "filename: id=cam123/second" in overwritten
    assert "filename: id=cam123/first" not in overwritten
