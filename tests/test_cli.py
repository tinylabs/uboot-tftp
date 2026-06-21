from openipc_tftp.cli import build_parser, build_server
from openipc_tftp.config import load_daemon_config
import pytest


def test_cli_accepts_only_config_path():
    args = build_parser().parse_args(["config.toml"])

    assert args.config == "config.toml"


def test_build_server_uses_server_config(tmp_path):
    script = tmp_path / "script.py"
    script.write_text(
        "async def default(tftp, ident, cmd, env):\n"
        "    await tftp.exec(['echo ok'], final=True)\n"
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            (
                "[server]",
                'scriptfile = "script.py"',
                'root = "tftp-root"',
                "port = 7070",
                "retries = 4",
                "timeout = 8",
                "",
                "[env]",
                'rambase = "baseaddr"',
                'cmdtftp = "tftpboot"',
                'cmdtftpput = "tftpput"',
                "",
                "[default]",
                'script = "default"',
            )
        )
    )

    server = build_server(load_daemon_config(config_path))

    assert server.port == 7070
    assert server.retries == 4
    assert server.timeout == 8
    assert server.tftproot == str((tmp_path / "tftp-root").resolve())


def test_load_daemon_config_requires_transport_keys_in_base_env(tmp_path):
    script = tmp_path / "script.py"
    script.write_text(
        "async def default(tftp, ident, cmd, env):\n"
        "    await tftp.exec(['echo ok'], final=True)\n"
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            (
                "[server]",
                'scriptfile = "script.py"',
                "",
                "[env]",
                'rambase = "baseaddr"',
                "",
                "[default]",
                'script = "default"',
            )
        )
    )

    with pytest.raises(ValueError, match=r"\[env\] must define: cmdtftp, cmdtftpput"):
        load_daemon_config(config_path)
