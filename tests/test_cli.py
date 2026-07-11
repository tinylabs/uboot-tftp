import logging
from pathlib import Path

from uboot_tftp.cli import (
    build_parser,
    build_runtime,
    build_server,
    configure_logging,
    find_config_pids,
    install_reload_handler,
    pidfile_path,
    remove_pidfile,
    reload_server,
    resolve_reload_pid,
    write_pidfile,
)
from uboot_tftp.config import load_daemon_config
import pytest


def test_cli_accepts_only_config_path():
    args = build_parser().parse_args(["--config", "config.toml"])

    assert args.config == "config.toml"
    assert args.rootdir is None
    assert args.log_dir is None


def test_cli_accepts_log_dir():
    args = build_parser().parse_args(
        ["--config", "config.toml", "--log-dir", "/tmp/uboot-logs"]
    )

    assert args.log_dir == "/tmp/uboot-logs"


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
                f'rootdir = "{(tmp_path / "tftp-root").resolve()}"',
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
                'entry_func = "default"',
            )
        )
    )

    server = build_server(load_daemon_config(config_path))

    assert server.port == 7070
    assert server.retries == 4
    assert server.timeout == 8
    assert server.tftproot == str((tmp_path / "tftp-root").resolve())


def test_build_server_allows_rootdir_override(tmp_path):
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
                f'rootdir = "{(tmp_path / "config-root").resolve()}"',
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

    server = build_server(
        load_daemon_config(config_path, rootdir=(tmp_path / "cli-root").resolve())
    )

    assert server.tftproot == str((tmp_path / "cli-root").resolve())


def test_build_runtime_returns_fresh_provider_and_upload_store(tmp_path):
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

    provider, uploads = build_runtime(
        load_daemon_config(config_path),
        log_dir=(tmp_path / "logs").resolve(),
    )

    assert provider.static_root == (tmp_path / "root").resolve()
    assert provider.upload_store is uploads
    assert provider.session_log_dir == (tmp_path / "logs").resolve()


def test_pidfile_path_defaults_to_config_basename(tmp_path):
    script = tmp_path / "script.py"
    script.write_text("async def default(tftp, ident, cmd, env):\n    pass\n")
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

    config = load_daemon_config(config_path)

    assert pidfile_path(config) == config_path.resolve().with_suffix(".pid")


def test_write_and_remove_pidfile_round_trip(tmp_path):
    path = tmp_path / "uboot-tftp.pid"

    written = write_pidfile(path, 4321)

    assert written == path.resolve()
    assert path.read_text() == "4321\n"

    remove_pidfile(path)

    assert not path.exists()


def test_find_config_pids_scans_proc_cmdlines(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = (tmp_path / "config.toml").resolve()
    proc1 = proc_root / "1001"
    proc1.mkdir()
    (proc1 / "cmdline").write_bytes(
        b"python\x00-m\x00uboot_tftp.cli\x00--config\x00" + str(config_path).encode() + b"\x00"
    )
    proc2 = proc_root / "1002"
    proc2.mkdir()
    (proc2 / "cmdline").write_bytes(b"python\x00other.py\x00")
    proc3 = proc_root / "1003"
    proc3.mkdir()
    (proc3 / "cmdline").write_bytes(
        b"python\x00/home/elliot/.venv/bin/uboot-tftp-check\x00--config\x00"
        + str(config_path).encode()
        + b"\x00"
    )

    assert find_config_pids(config_path, proc_root=proc_root, current_pid=9999) == [1001]


def test_find_config_pids_ignores_current_process(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    config_path = (tmp_path / "config.toml").resolve()
    proc1 = proc_root / "1001"
    proc1.mkdir()
    (proc1 / "cmdline").write_bytes(
        b"python\x00/home/elliot/.venv/bin/uboot-tftp\x00--config\x00"
        + str(config_path).encode()
        + b"\x00"
    )

    assert find_config_pids(config_path, proc_root=proc_root, current_pid=1001) == []


def test_resolve_reload_pid_uses_pidfile_when_available(tmp_path):
    script = tmp_path / "script.py"
    script.write_text("async def default(tftp, ident, cmd, env):\n    pass\n")
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
    config = load_daemon_config(config_path)
    write_pidfile(pidfile_path(config), 9876)

    assert resolve_reload_pid(config, proc_root=tmp_path / "missing-proc") == 9876


def test_resolve_reload_pid_rejects_multiple_matching_instances(tmp_path):
    script = tmp_path / "script.py"
    script.write_text("async def default(tftp, ident, cmd, env):\n    pass\n")
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
    config = load_daemon_config(config_path)
    write_pidfile(pidfile_path(config), 1111)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    proc2 = proc_root / "2222"
    proc2.mkdir()
    (proc2 / "cmdline").write_bytes(
        b"python\x00-m\x00uboot_tftp.cli\x00--config\x00" + str(config.path).encode() + b"\x00"
    )

    with pytest.raises(ValueError, match="multiple running instances found"):
        resolve_reload_pid(config, proc_root=proc_root)


def test_load_daemon_config_rejects_relative_rootdir(tmp_path):
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
                'rootdir = "tftp-root"',
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

    with pytest.raises(ValueError, match=r"\[server\] rootdir must be an absolute path"):
        load_daemon_config(config_path)


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
                'entry_func = "default"',
            )
        )
    )

    with pytest.raises(ValueError, match=r"\[env\] must define: cmdtftp, cmdtftpput"):
        load_daemon_config(config_path)


def test_configure_logging_keeps_tftpy_quiet_at_info():
    root = logging.getLogger()
    tftpy_logger = logging.getLogger("tftpy")
    original_root_handlers = list(root.handlers)
    original_root_level = root.level
    original_tftpy_level = tftpy_logger.level
    try:
        root.handlers.clear()
        root.setLevel(logging.NOTSET)
        tftpy_logger.setLevel(logging.NOTSET)

        level = configure_logging("INFO")

        assert level == logging.INFO
        assert tftpy_logger.level == logging.WARNING
    finally:
        root.handlers[:] = original_root_handlers
        root.setLevel(original_root_level)
        tftpy_logger.setLevel(original_tftpy_level)


def test_configure_logging_keeps_tftpy_verbose_at_debug():
    root = logging.getLogger()
    tftpy_logger = logging.getLogger("tftpy")
    original_root_handlers = list(root.handlers)
    original_root_level = root.level
    original_tftpy_level = tftpy_logger.level
    try:
        root.handlers.clear()
        root.setLevel(logging.NOTSET)
        tftpy_logger.setLevel(logging.NOTSET)

        level = configure_logging("DEBUG")

        assert level == logging.DEBUG
        assert tftpy_logger.level == logging.DEBUG
    finally:
        root.handlers[:] = original_root_handlers
        root.setLevel(original_root_level)
        tftpy_logger.setLevel(original_tftpy_level)


def test_reload_server_reloads_config_and_user_script(tmp_path):
    script = tmp_path / "script.py"
    script.write_text(
        "async def default(tftp, ident, cmd, env):\n"
        "    await tftp.exec(['echo old'], final=True)\n"
    )
    config_path = tmp_path / "config.toml"
    root1 = (tmp_path / "root1").resolve()
    root2 = (tmp_path / "root2").resolve()
    config_path.write_text(
        "\n".join(
            (
                "[server]",
                'scriptfile = "script.py"',
                f'rootdir = "{root1}"',
                "timeout = 5",
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
    server = build_server(load_daemon_config(config_path))

    script.write_text(
        "async def default(tftp, ident, cmd, env):\n"
        "    await tftp.exec(['echo new'], final=True)\n"
    )
    config_path.write_text(
        "\n".join(
            (
                "[server]",
                'scriptfile = "script.py"',
                f'rootdir = "{root2}"',
                "timeout = 9",
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

    reload_server(server, config_path=str(config_path))

    assert server.tftproot == str(root2)
    assert server.timeout == 9
    module_path = Path(server.provider._module.__file__).resolve()
    assert module_path == script.resolve()
    assert server.provider.static_root == root2


def test_install_reload_handler_registers_sighup_callback(tmp_path, caplog):
    class FakeSignalModule:
        SIGHUP = 1

        def __init__(self):
            self.calls = []

        def signal(self, signum, handler):
            self.calls.append((signum, handler))

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
    server = build_server(load_daemon_config(config_path))
    fake_signal = FakeSignalModule()

    handler = install_reload_handler(
        server,
        config_path=str(config_path),
        signal_module=fake_signal,
    )

    assert handler is not None
    assert fake_signal.calls == [(1, handler)]

    with caplog.at_level(logging.INFO):
        handler(1, None)

    assert "Reloaded configuration from" in caplog.text


def test_install_reload_handler_logs_reload_failure_and_keeps_running(tmp_path, caplog):
    class FakeSignalModule:
        SIGHUP = 1

        def __init__(self):
            self.calls = []

        def signal(self, signum, handler):
            self.calls.append((signum, handler))

    script = tmp_path / "script.py"
    script.write_text(
        "async def default(tftp, ident, cmd, env):\n"
        "    await tftp.exec(['echo ok'], final=True)\n"
    )
    config_path = tmp_path / "config.toml"
    root = (tmp_path / "root").resolve()
    config_path.write_text(
        "\n".join(
            (
                "[server]",
                'scriptfile = "script.py"',
                f'rootdir = "{root}"',
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
    server = build_server(load_daemon_config(config_path))
    fake_signal = FakeSignalModule()
    handler = install_reload_handler(
        server,
        config_path=str(config_path),
        signal_module=fake_signal,
    )
    config_path.write_text("not toml")

    with caplog.at_level(logging.INFO):
        handler(1, None)

    assert server.tftproot == str(root)
    assert "Failed to reload configuration from" in caplog.text
