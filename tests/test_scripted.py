import pytest
import re

from openipc_tftp.config import load_daemon_config
from openipc_tftp.mkimage import extract_script_payload
from openipc_tftp.providers import ContentRequest
from openipc_tftp.scripted import ScriptedSessionProvider
from openipc_tftp.sessions import InMemorySessionStore
from openipc_tftp.uploads import InMemoryUploadStore, UploadRequest


def script_from_result(result):
    return extract_script_payload(result.body).decode("utf-8")


TOKEN_RE = re.compile(r'id=cam123/token=([^"/]+)')


def request(filename):
    return ContentRequest(
        filename=filename,
        peer=("127.0.0.1", 12345),
        server_addr=("127.0.0.1", 6969),
        options={"mode": "octet"},
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
                'root = "static"',
                "",
                "[env]",
                'rambase = "loadaddr"',
                'cmdtftp = "tftpboot"',
                'cmdtftpput = "tftpput"',
                "",
                "[cam123]",
                f'script = "{route}"',
                "",
                "[default]",
                'script = "default"',
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

    assert "echo known cam123 boot -" in script_from_result(provider.fetch(request("id=cam123/boot")))
    assert "echo default other123 boot" in script_from_result(
        provider.fetch(request("id=other123/boot"))
    )


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

    first = script_from_result(provider.fetch(request("id=cam123/bootstrap")))
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

    first = script_from_result(provider.fetch(request("id=cam123/bootstrap")))
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert '/filesize=${filesize}"' in first

    second = script_from_result(
        provider.fetch(request(f"id=cam123/token={token}/filesize=1235"))
    )
    assert "echo filesize 1235" in second


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
                'root = "static"',
                "",
                "[env]",
                'rambase = "baseaddr"',
                'cmdtftp = "tftpboot"',
                'cmdtftpput = "tftpput"',
                "",
                "[cam123]",
                'script = "handler"',
                'rambase = "loadaddr"',
                'cmdtftp = "dhcp"',
                'cmdtftpput = "nmrp"',
                'extra = "route"',
                "",
                "[default]",
                'script = "default"',
            )
        )
    )
    config = load_daemon_config(config_path)
    sessions = InMemorySessionStore()
    uploads = InMemoryUploadStore(sessions)
    provider = ScriptedSessionProvider(config, sessions=sessions, upload_store=uploads)

    first = script_from_result(provider.fetch(request("id=cam123/bootstrap/extra=rrq")))
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

    first = script_from_result(provider.fetch(request("id=cam123/bootstrap")))
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

    first = script_from_result(provider.fetch(request("id=cam123/bootstrap")))
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert "setexpr __openipc_tftp_recv_" in first
    assert " ${loadaddr} + 0x400" in first
    recv_tmp = next(
        line.split()[1]
        for line in first.splitlines()
        if line.startswith("setexpr __openipc_tftp_recv_")
    )
    assert (
        f'tftpput ${{{recv_tmp}}} 0x8 "127.0.0.1:id=cam123/token={token}/upload.bin"' in first
    )
    assert f"setenv {recv_tmp}" in first


def test_exec_recv_can_be_caught_by_user_script(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "from openipc_tftp.scripted import ReceiveFailedError",
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

    first = script_from_result(provider.fetch(request("id=cam123/bootstrap")))
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

    with pytest.raises(ValueError, match="final=True"):
        provider.fetch(request("id=cam123/bootstrap"))


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

    script = script_from_result(
        provider.fetch(request("id=cam123/bootstrap/host=rrq/mode=rrq-mode"))
    )
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

    script = script_from_result(provider.fetch(request("id=cam123/bootstrap")))
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

    first = script_from_result(provider.fetch(request("id=cam123/bootstrap")))
    token_match = TOKEN_RE.search(first)
    assert token_match is not None
    token = token_match.group(1)
    assert f"env export -t ${{loadaddr}}" in first
    assert f'/filesize=${{filesize}}"' in first

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
