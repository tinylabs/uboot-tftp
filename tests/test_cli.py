import pytest

from openipc_tftp.cli import build_parser, parse_report, parse_set_var


def test_parse_set_var_accepts_name_value():
    assert parse_set_var("bootdelay=3") == ("bootdelay", "3")


def test_parse_set_var_rejects_missing_separator():
    with pytest.raises(Exception, match="NAME=VALUE"):
        parse_set_var("bootdelay")


def test_parse_report_accepts_name_expression():
    assert parse_report("filesize=${filesize}") == ("filesize", "${filesize}")


def test_cli_parses_repeated_helper_flags():
    args = build_parser().parse_args(
        [
            "--ethaddr",
            "aa:bb:cc:dd:ee:ff",
            "--get-var",
            "ipaddr",
            "--get-var",
            "serverip",
            "--set-var",
            "bootdelay=3",
            "--saveenv",
        ]
    )

    assert args.ethaddr == "aa:bb:cc:dd:ee:ff"
    assert args.get_var == ["ipaddr", "serverip"]
    assert args.set_var == [("bootdelay", "3")]
    assert args.saveenv is True


def test_cli_parses_extended_primitives():
    args = build_parser().parse_args(
        [
            "--run-var",
            "bootcmd",
            "--run-cmd",
            "echo one",
            "--run-cmd",
            "echo two",
            "--run-name",
            "smoke",
            "--printenv",
            "--printenv-var",
            "ipaddr",
            "--probe",
            "--sleep",
            "2",
            "--report",
            "filesize=${filesize}",
            "--boot",
            "bootm ${loadaddr}",
            "--reset",
            "--upload-dir",
            "/tmp/uploads",
            "--export-env",
            "upload/full-env.txt",
            "--export-env-addr",
            "0x43000000",
        ]
    )

    assert args.run_var == ["bootcmd"]
    assert args.run_cmd == ["echo one", "echo two"]
    assert args.run_name == "smoke"
    assert args.printenv is True
    assert args.printenv_var == ["ipaddr"]
    assert args.probe is True
    assert args.sleep == [2]
    assert args.report == [("filesize", "${filesize}")]
    assert args.boot == "bootm ${loadaddr}"
    assert args.reset is True
    assert args.upload_dir == "/tmp/uploads"
    assert args.export_env == "upload/full-env.txt"
    assert args.export_env_addr == "0x43000000"
