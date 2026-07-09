import signal

import pytest

import uboot_tftp.check_cli as check_cli
from uboot_tftp.check_cli import build_parser, main
from uboot_tftp.cli import write_pidfile
from uboot_tftp.config import check_user_script_syntax


def test_check_cli_accepts_config_path():
    args = build_parser().parse_args(["--config", "config.toml"])

    assert args.config == "config.toml"
    assert args.rootdir is None
    assert args.reload is False
    assert args.pid is None


def test_check_user_script_syntax_accepts_valid_python(tmp_path):
    script = tmp_path / "script.py"
    script.write_text(
        "async def default(tftp, ident, cmd, env):\n"
        "    await tftp.exec(['echo ok'], final=True)\n"
    )

    assert check_user_script_syntax(script) == script.resolve()


def test_check_user_script_syntax_reports_syntax_error(tmp_path):
    script = tmp_path / "script.py"
    script.write_text("async def default(:\n    pass\n")

    with pytest.raises(ValueError, match=r"python syntax error .* line 1"):
        check_user_script_syntax(script)


def test_check_user_script_syntax_reports_missing_file(tmp_path):
    script = tmp_path / "missing.py"

    with pytest.raises(ValueError, match=r"script file not found: .*missing\.py"):
        check_user_script_syntax(script)


def test_check_cli_validates_config_and_script(tmp_path, capsys):
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
                f'rootdir = "{(tmp_path / "root").resolve()}"',
                "",
                "[env]",
                'rambase = "baseaddr"',
                'cmdtftp = "tftpboot"',
                'cmdtftpput = "tftpput"',
                "",
                "[default]",
                'entry_func = "default"',
            )
        )
    )

    exit_code = main(["--config", str(config_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert f"Config OK: {config_path.resolve()}" in output
    assert f"Script OK: {script.resolve()}" in output


def test_check_cli_surfaces_script_syntax_errors(tmp_path, capsys):
    script = tmp_path / "script.py"
    script.write_text("async def default(:\n    pass\n")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            (
                "[server]",
                'scriptfile = "script.py"',
                f'rootdir = "{(tmp_path / "root").resolve()}"',
                "",
                "[env]",
                'rambase = "baseaddr"',
                'cmdtftp = "tftpboot"',
                'cmdtftpput = "tftpput"',
                "",
                "[default]",
                'entry_func = "default"',
            )
        )
    )

    exit_code = main(["--config", str(config_path)])

    assert exit_code == 1
    assert "python syntax error" in capsys.readouterr().err


def test_check_cli_surfaces_missing_script_file(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            (
                "[server]",
                'scriptfile = "missing.py"',
                f'rootdir = "{(tmp_path / "root").resolve()}"',
                "",
                "[env]",
                'rambase = "baseaddr"',
                'cmdtftp = "tftpboot"',
                'cmdtftpput = "tftpput"',
                "",
                "[default]",
                'entry_func = "default"',
            )
        )
    )

    exit_code = main(["--config", str(config_path)])

    assert exit_code == 1
    assert "script file not found:" in capsys.readouterr().err


def test_check_cli_rejects_unknown_server_keys(tmp_path, capsys):
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
                f'rootdir = "{(tmp_path / "root").resolve()}"',
                'address = "0.0.0.0"',
                "port = 69",
                "timeout = 5",
                "retries = 3",
                'log_level = "info"',
                'invalid_arg = "true"',
                "",
                "[env]",
                'rambase = "baseaddr"',
                'cmdtftp = "tftpboot"',
                'cmdtftpput = "tftpput"',
                "",
                "[default]",
                'entry_func = "default"',
            )
        )
    )

    exit_code = main(["--config", str(config_path)])

    assert exit_code == 1
    assert "[server] unknown keys: invalid_arg" in capsys.readouterr().err


def test_check_cli_rejects_invalid_server_value_types(tmp_path, capsys):
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
                f'rootdir = "{(tmp_path / "root").resolve()}"',
                'port = "69"',
                "",
                "[env]",
                'rambase = "baseaddr"',
                'cmdtftp = "tftpboot"',
                'cmdtftpput = "tftpput"',
                "",
                "[default]",
                'entry_func = "default"',
            )
        )
    )

    exit_code = main(["--config", str(config_path)])

    assert exit_code == 1
    assert "[server] port must be an integer" in capsys.readouterr().err


def test_check_cli_can_send_reload_signal_after_validation(tmp_path, monkeypatch, capsys):
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
                f'rootdir = "{(tmp_path / "root").resolve()}"',
                "",
                "[env]",
                'rambase = "baseaddr"',
                'cmdtftp = "tftpboot"',
                'cmdtftpput = "tftpput"',
                "",
                "[default]",
                'entry_func = "default"',
            )
        )
    )
    seen = {}
    write_pidfile(config_path.with_suffix(".pid"), 1234)

    def fake_kill(pid, signum):
        seen["pid"] = pid
        seen["signum"] = signum

    monkeypatch.setattr(check_cli.os, "kill", fake_kill)

    exit_code = main(["--config", str(config_path), "--reload"])

    assert exit_code == 0
    assert seen == {"pid": 1234, "signum": signal.SIGHUP}
    output = capsys.readouterr().out
    assert "Reload signal sent: pid=1234 signal=SIGHUP" in output


def test_check_cli_reload_errors_when_multiple_instances_match(tmp_path, monkeypatch, capsys):
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
                f'rootdir = "{(tmp_path / "root").resolve()}"',
                "",
                "[env]",
                'rambase = "baseaddr"',
                'cmdtftp = "tftpboot"',
                'cmdtftpput = "tftpput"',
                "",
                "[default]",
                'entry_func = "default"',
            )
        )
    )

    monkeypatch.setattr(check_cli, "resolve_reload_pid", lambda config, explicit_pid=None: (_ for _ in ()).throw(ValueError("multiple running instances found for config")))

    exit_code = main(["--config", str(config_path), "--reload"])

    assert exit_code == 1
    assert "multiple running instances found for config" in capsys.readouterr().err
