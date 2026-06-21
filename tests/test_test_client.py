import pytest

from openipc_tftp.test_client import (
    _client_env,
    _find_continue_remote,
    _find_upload_remote,
    _remote_filename,
    _substitute_uboot_vars,
)


def test_client_extracts_upload_and_continue_remotes():
    script = (
        'if tftpput ${ramaddr} ${filesize} "${serverip}:'
        'id=cam123/upload/env.txt"; then\n'
        'if dhcp ${ramaddr} "${serverip}:id=cam123/bootstrap"; '
        "then source ${ramaddr}; fi"
    )
    env = _client_env("cam123", {})

    assert _find_upload_remote(script, env) == "id=cam123/upload/env.txt"
    assert _find_continue_remote(script, env) == "id=cam123/bootstrap"


def test_client_substitutes_explicit_env_values():
    value = _substitute_uboot_vars(
        "id=${serial#}/var/ipaddr=${ipaddr}",
        {"serial#": "cam123", "ipaddr": "192.168.1.50"},
    )

    assert value == "id=cam123/var/ipaddr=192.168.1.50"


def test_client_rejects_missing_env_values():
    with pytest.raises(SystemExit, match="ipaddr"):
        _substitute_uboot_vars("id=${serial#}/var/ipaddr=${ipaddr}", {"serial#": "cam123"})


def test_remote_filename_normalizes_id_path():
    assert _remote_filename("cam123", "/boot") == "id=cam123/boot"
