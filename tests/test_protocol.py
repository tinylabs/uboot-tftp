import pytest

from uboot_tftp.protocol import parse_request_path


def test_parse_session_request_path():
    parsed = parse_request_path("id=CAM123/bootstrap/board=hi3536")

    assert parsed.client_id == "cam123"
    assert parsed.is_session is True
    assert parsed.path == "/bootstrap/board=hi3536"
    assert parsed.values == {"board": "hi3536"}


def test_parse_static_request_path():
    parsed = parse_request_path("images/uImage")

    assert parsed.client_id is None
    assert parsed.is_session is False
    assert parsed.path == "/images/uImage"


def test_parse_root_request_path():
    parsed = parse_request_path("/")

    assert parsed.client_id is None
    assert parsed.path == "/"


def test_parse_session_request_path_allows_at_command_segment():
    parsed = parse_request_path("id=cam123/@probe/mode=fast")

    assert parsed.client_id == "cam123"
    assert parsed.segments[0] == "@probe"
    assert parsed.path == "/@probe/mode=fast"
    assert parsed.values == {"mode": "fast"}


def test_parse_rejects_invalid_client_id():
    with pytest.raises(ValueError, match="invalid client id"):
        parse_request_path("id=cam.123/bootstrap")
