from openipc_tftp.config import load_daemon_config
from openipc_tftp.mkimage import extract_script_payload
from openipc_tftp.providers import ContentRequest
from openipc_tftp.scripted import ScriptedConfigProvider
from openipc_tftp.uploads import InMemoryUploadStore, UploadRequest


def script_from_result(result):
    return extract_script_payload(result.body).decode("utf-8")


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
                "",
                "[env]",
                'bootcmd = "boot"',
                'base = "toml"',
                'cmdtftp = "tftpboot"',
                'ramvar = "ramaddr"',
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
                "def handler(uboot, ident, path):",
                "    uboot.send_noreply(f'known {ident} {path}')",
                "",
                "def default(uboot, ident, path):",
                "    uboot.send_noreply(f'default {ident} {path}')",
            )
        ),
    )
    provider = ScriptedConfigProvider(config, upload_store=InMemoryUploadStore())

    assert "known cam123 /boot" in script_from_result(provider.fetch(request("id=cam123/boot")))
    assert "default other123 /boot" in script_from_result(
        provider.fetch(request("id=other123/boot"))
    )


def test_send_wraps_script_with_continue_rrq(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "def handler(uboot, ident, path):",
                "    uboot.send('echo hello')",
                "",
                "def default(uboot, ident, path):",
                "    uboot.send_noreply('default')",
            )
        ),
    )
    provider = ScriptedConfigProvider(config, upload_store=InMemoryUploadStore())

    script = script_from_result(provider.fetch(request("id=cam123/bootstrap")))

    assert "echo hello" in script
    assert 'if tftpboot ${ramaddr} "${serverip}:id=cam123/bootstrap"' in script
    assert "then source ${ramaddr};" in script


def test_get_env_requests_upload_then_merges_with_config_env(tmp_path):
    config = write_config(
        tmp_path,
        "\n".join(
            (
                "def handler(uboot, ident, path):",
                "    env = uboot.get_env()",
                "    uboot.send_noreply(f\"{env['base']} {env['bootcmd']} {env['ipaddr']}\")",
                "",
                "def default(uboot, ident, path):",
                "    uboot.send_noreply('default')",
            )
        ),
    )
    uploads = InMemoryUploadStore()
    provider = ScriptedConfigProvider(config, upload_store=uploads)

    first = script_from_result(provider.fetch(request("id=cam123/bootstrap")))
    assert "env export -t ${ramaddr}" in first
    assert 'if tftpput ${ramaddr} ${filesize} "${serverip}:' in first
    assert 'id=cam123/upload/env.txt' in first
    assert 'id=cam123/bootstrap' in first

    upload = uploads.open(
        UploadRequest(
            filename="id=cam123/upload/env.txt",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
        )
    )
    upload.write(b"bootcmd=run target\nipaddr=192.168.1.50\n")
    upload.close()

    second = script_from_result(provider.fetch(request("id=cam123/bootstrap")))
    assert "toml run target 192.168.1.50" in second
