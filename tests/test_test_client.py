from openipc_tftp.mkimage import LegacyScriptImageCompiler
from openipc_tftp.test_client import (
    ClientConfig,
    DownloadAction,
    FlowActions,
    UploadAction,
    _build_dummy_env_export,
    _build_dummy_env_export_unpadded,
    choose_next_remote,
    main,
    parse_flow_actions,
    run_client,
)


def test_test_client_prints_extracted_script(tmp_path, capsys):
    image = tmp_path / "boot.uimg"
    image.write_bytes(LegacyScriptImageCompiler().compile("echo hello\n"))

    assert main([str(image)]) == 0
    assert capsys.readouterr().out == "echo hello\n\n"


def test_parse_flow_actions_finds_upload_and_continuation():
    script = "\n".join(
        (
            "echo start",
            'if tftpput ${loadaddr} 8 "127.0.0.1:id=cam123/token=abc123/upload.bin"; then '
            'if tftpboot ${loadaddr} "127.0.0.1:id=cam123/token=abc123/recv=ok"; '
            "then source ${loadaddr}; fi "
            'else if tftpboot ${loadaddr} "127.0.0.1:id=cam123/token=abc123/recv=failed"; '
            "then source ${loadaddr}; fi fi",
        )
    )

    actions = parse_flow_actions(script)

    assert actions.uploads == (
        UploadAction(
            command="tftpput",
            server="127.0.0.1",
            remote="id=cam123/token=abc123/upload.bin",
            size=8,
        ),
    )
    assert actions.downloads == (
        DownloadAction(
            command="tftpboot",
            server="127.0.0.1",
            remote="id=cam123/token=abc123/recv=ok",
        ),
        DownloadAction(
            command="tftpboot",
            server="127.0.0.1",
            remote="id=cam123/token=abc123/recv=failed",
        ),
    )
    assert choose_next_remote(actions, {}, prefer_recv="ok") == "id=cam123/token=abc123/recv=ok"


def test_run_client_follows_rrq_wrq_flow(capsys, tmp_path):
    first = LegacyScriptImageCompiler().compile(
        "\n".join(
            (
                "echo first",
                'if tftpput ${loadaddr} 128 "127.0.0.1:id=cam123/token=abc123/upload.bin"; '
                'then if tftpboot ${loadaddr} "127.0.0.1:id=cam123/token=abc123/recv=ok"; '
                'then source ${loadaddr}; fi else echo failed; fi',
            )
        )
        + "\n"
    )
    second = LegacyScriptImageCompiler().compile("echo done\n")

    transfers: list[tuple[str, str, bytes | None]] = []

    class FakeClient:
        def __init__(self, host, port=69):
            self.host = host
            self.port = port

        def download(self, filename, output, packethook=None, timeout=5, retries=3):
            transfers.append(("download", filename, None))
            payload = first if filename.endswith("/bootstrap") else second
            with open(output, "wb") as fileobj:
                fileobj.write(payload)

        def upload(self, filename, input, packethook=None, timeout=5, retries=3):
            transfers.append(("upload", filename, input.read()))

    config = ClientConfig(
        host="127.0.0.1",
        port=6969,
        client_id="cam123",
        path="/bootstrap",
        rounds=3,
        timeout=5,
        retries=3,
        keep_dir=tmp_path,
        dummy_byte=ord("Z"),
    )

    run_client(config, client_factory=FakeClient)

    expected_upload = _build_dummy_env_export(config, 128)
    assert transfers == [
        ("download", "id=cam123/bootstrap", None),
        ("upload", "id=cam123/token=abc123/upload.bin", expected_upload),
        ("download", "id=cam123/token=abc123/recv=ok", None),
    ]
    output = capsys.readouterr().out
    assert "RRQ 1: id=cam123/bootstrap" in output
    assert "WRQ 1: id=cam123/token=abc123/upload.bin (128 bytes via tftpput)" in output
    assert "No continuation RRQ found; stopping." in output


def test_choose_next_remote_falls_back_to_first_download():
    actions = FlowActions(
        uploads=(),
        downloads=(
            DownloadAction(
                command="tftpboot",
                server="127.0.0.1",
                remote="id=cam123/token=abc123",
            ),
            DownloadAction(
                command="tftpboot",
                server="127.0.0.1",
                remote="id=cam123/token=def456",
            ),
        ),
    )

    assert choose_next_remote(actions, {}) == "id=cam123/token=abc123"


def test_choose_next_remote_substitutes_filesize_variable():
    actions = FlowActions(
        uploads=(),
        downloads=(
            DownloadAction(
                command="tftpboot",
                server="127.0.0.1",
                remote="id=cam123/token=abc123/filesize=${filesize}",
            ),
        ),
    )

    assert choose_next_remote(actions, {"filesize": "106"}) == (
        "id=cam123/token=abc123/filesize=106"
    )


def test_dummy_env_export_unpadded_length_matches_expected_filesize():
    config = ClientConfig(
        host="127.0.0.1",
        port=6969,
        client_id="cam123",
        path="/bootstrap",
        rounds=3,
        timeout=5,
        retries=3,
        keep_dir=None,
        dummy_byte=ord("Z"),
    )

    assert len(_build_dummy_env_export_unpadded(config)) == 106
