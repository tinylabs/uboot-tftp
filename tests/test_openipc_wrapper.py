from pathlib import Path

import uboot_tftp.openipc_tftp as openipc_tftp


def test_openipc_tftp_wrapper_forwards_packaged_config(monkeypatch):
    seen = {}

    def fake_cli_main(argv):
        seen["argv"] = argv
        return 7

    monkeypatch.setattr(openipc_tftp, "cli_main", fake_cli_main)

    result = openipc_tftp.main(["--rootdir", "/tmp/example-root"])

    assert result == 7
    assert seen["argv"][0] == "--config"
    assert Path(seen["argv"][1]).name == "openipc.toml"
    assert seen["argv"][2:] == ["--rootdir", "/tmp/example-root"]
