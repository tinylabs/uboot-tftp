import pytest

from openipc_tftp.protocol import parse_client_filename


def test_parse_bootstrap_filename():
    message = parse_client_filename("id=CAM123/")

    assert message.client_id == "cam123"
    assert message.channel == "bootstrap"
    assert message.segments == ()
    assert message.values == {}


def test_parse_client_id():
    message = parse_client_filename("id=cam123/")

    assert message.client_id == "cam123"


def test_parse_hyphenated_client_id():
    message = parse_client_filename("id=cam-123/")

    assert message.client_id == "cam-123"


def test_parse_underscored_client_id():
    message = parse_client_filename("id=cam_123/")

    assert message.client_id == "cam_123"


def test_parse_env_filename_values():
    message = parse_client_filename(
        "id=cam123/env/ipaddr=192.168.1.50/serial=abc123"
    )

    assert message.channel == "env"
    assert message.values == {
        "ipaddr": "192.168.1.50",
        "serial": "abc123",
    }


def test_parse_rejects_missing_id_prefix():
    with pytest.raises(ValueError, match="id"):
        parse_client_filename("env/ipaddr=192.168.1.50")


def test_parse_rejects_invalid_client_id():
    with pytest.raises(ValueError, match="invalid client id"):
        parse_client_filename("id=cam.123/env")
