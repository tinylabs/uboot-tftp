from uboot_tftp import CallableContentProvider, ContentResult
from types import SimpleNamespace

from tftpy.TftpPacketTypes import TftpPacketWRQ
from tftpy.TftpStates import TftpServerState, TftpState

from uboot_tftp.server import DynamicContentServer, fileobj_from_result
from uboot_tftp.sessions import InMemorySessionStore, PendingReceive
from uboot_tftp.uploads import InMemoryUploadStore


class FakeTftpServer:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.listen_args = None
        self.stopped = False

    def listen(self, **kwargs):
        self.listen_args = kwargs

    def stop(self, now=False):
        self.stopped = now


def test_dynamic_content_server_passes_rrq_context_to_provider(tmp_path):
    seen = {}

    def fetch(request):
        seen["request"] = request
        return ContentResult.from_bytes(b"ok")

    server = DynamicContentServer(
        address="127.0.0.1",
        port=6969,
        retries=3,
        timeout=5,
        provider=CallableContentProvider(fetch),
        upload_store=InMemoryUploadStore(InMemorySessionStore()),
        tftproot=tmp_path,
        server_factory=FakeTftpServer,
    )
    fileobj = server._open_dynamic_download(
        "camera/firmware.bin",
        raddress="127.0.0.1",
        rport=12345,
    )

    assert seen["request"].filename == "camera/firmware.bin"
    assert seen["request"].peer == ("127.0.0.1", 12345)
    assert fileobj.read() == b"ok"


def test_dynamic_content_server_logs_rrq_summary(tmp_path, caplog):
    server = DynamicContentServer(
        address="127.0.0.1",
        port=6969,
        retries=3,
        timeout=5,
        provider=CallableContentProvider(lambda request: ContentResult.from_bytes(b"ok")),
        upload_store=InMemoryUploadStore(InMemorySessionStore()),
        tftproot=tmp_path,
        server_factory=FakeTftpServer,
    )

    with caplog.at_level("INFO"):
        fileobj = server._open_dynamic_download(
            "camera/firmware.bin",
            raddress="127.0.0.1",
            rport=12345,
        )
        fileobj.read()
        fileobj.close()

    assert "RRQ filename=camera/firmware.bin peer=127.0.0.1:12345" in caplog.text
    assert (
        "RRQ complete filename=camera/firmware.bin peer=127.0.0.1:12345 bytes=2"
        in caplog.text
    )


def test_dynamic_content_server_opens_expected_session_upload_sink(tmp_path):
    sessions = InMemorySessionStore()
    session = sessions.create("cam123")
    session.pending_receive = PendingReceive(
        token="abc123",
        upload_path="/upload.bin",
        size=8,
    )
    upload_store = InMemoryUploadStore(sessions)
    server = DynamicContentServer(
        address="127.0.0.1",
        port=6969,
        retries=3,
        timeout=5,
        provider=CallableContentProvider(lambda request: ContentResult.from_bytes(b"")),
        upload_store=upload_store,
        tftproot=tmp_path,
        server_factory=FakeTftpServer,
    )

    class Context:
        host = "127.0.0.1"
        port = 12345
        flock = True

    context = Context()
    upload = server._open_upload(
        str(tmp_path / "id=cam123" / "token=abc123" / "upload.bin"),
        context,
    )
    upload.write(b"payload")
    upload.close()

    assert context.flock is False
    assert upload_store.all()[0].body == b"payload"


def test_dynamic_content_server_logs_wrq_summary(tmp_path, caplog):
    upload_store = InMemoryUploadStore(InMemorySessionStore())
    server = DynamicContentServer(
        address="127.0.0.1",
        port=6969,
        retries=3,
        timeout=5,
        provider=CallableContentProvider(lambda request: ContentResult.from_bytes(b"")),
        upload_store=upload_store,
        tftproot=tmp_path,
        server_factory=FakeTftpServer,
    )

    class Context:
        host = "127.0.0.1"
        port = 12345
        flock = True

    with caplog.at_level("INFO"):
        upload = server._open_upload(str(tmp_path / "plain.txt"), Context())
        upload.write(b"payload")
        upload.close()

    assert "WRQ filename=plain.txt peer=127.0.0.1:12345" in caplog.text
    assert "WRQ complete filename=plain.txt peer=127.0.0.1:12345 bytes=7" in caplog.text


def test_dynamic_content_server_writes_static_uploads_to_disk(tmp_path):
    upload_store = InMemoryUploadStore(InMemorySessionStore())
    server = DynamicContentServer(
        address="127.0.0.1",
        port=6969,
        retries=3,
        timeout=5,
        provider=CallableContentProvider(lambda request: ContentResult.from_bytes(b"")),
        upload_store=upload_store,
        tftproot=tmp_path,
        server_factory=FakeTftpServer,
    )

    class Context:
        host = "127.0.0.1"
        port = 12345
        flock = True

    upload = server._open_upload(str(tmp_path / "plain.txt"), Context())
    upload.write(b"payload")
    upload.close()

    assert (tmp_path / "plain.txt").read_bytes() == b"payload"
    assert upload_store.all() == []


def test_dynamic_content_server_run_delegates_to_tftpy_listen(tmp_path):
    fake = None

    def factory(**kwargs):
        nonlocal fake
        fake = FakeTftpServer(**kwargs)
        return fake

    server = DynamicContentServer(
        address="127.0.0.1",
        port=6969,
        retries=7,
        timeout=9,
        provider=CallableContentProvider(lambda request: ContentResult.from_bytes(b"")),
        upload_store=InMemoryUploadStore(InMemorySessionStore()),
        tftproot=tmp_path,
        server_factory=factory,
    )

    server.run()

    assert fake.listen_args == {
        "listenip": "127.0.0.1",
        "listenport": 6969,
        "timeout": 9,
        "retries": 7,
    }


def test_dynamic_content_server_reload_swaps_provider_and_runtime_state(tmp_path):
    server = DynamicContentServer(
        address="127.0.0.1",
        port=6969,
        retries=3,
        timeout=5,
        provider=CallableContentProvider(lambda request: ContentResult.from_bytes(b"old")),
        upload_store=InMemoryUploadStore(InMemorySessionStore()),
        tftproot=tmp_path / "old-root",
        server_factory=FakeTftpServer,
    )
    new_uploads = InMemoryUploadStore(InMemorySessionStore())
    new_provider = CallableContentProvider(lambda request: ContentResult.from_bytes(b"new"))

    server.reload(
        provider=new_provider,
        upload_store=new_uploads,
        tftproot=tmp_path / "new-root",
        retries=9,
        timeout=11,
    )

    assert server.provider is new_provider
    assert server.upload_store is new_uploads
    assert server.tftproot == str((tmp_path / "new-root"))
    assert server.retries == 9
    assert server.timeout == 11
    assert (tmp_path / "new-root").is_dir()


def test_fileobj_from_result_wraps_bytes():
    fileobj = fileobj_from_result(ContentResult.from_bytes(b"payload"))

    assert fileobj.read() == b"payload"
    assert isinstance(fileobj.fileno(), int)


def test_tftpy_patch_accepts_timeout_option():
    state = object.__new__(TftpState)

    accepted = state.returnSupportedOptions({"blksize": "1024", "timeout": "7"})

    assert accepted["blksize"] == "1024"
    assert accepted["timeout"] == "7"


def test_tftpy_patch_applies_timeout_to_server_context(tmp_path):
    class FakeSocket:
        def __init__(self):
            self.timeout = None

        def settimeout(self, value):
            self.timeout = value

    context = SimpleNamespace(
        tidport=None,
        options=None,
        host="127.0.0.1",
        port=12345,
        root=str(tmp_path),
        dyn_file_func=None,
        upload_open=lambda path, context: None,
        sock=FakeSocket(),
        file_to_transfer=None,
    )
    state = TftpServerState(context)
    pkt = TftpPacketWRQ()
    pkt.filename = "upload.bin"
    pkt.mode = "octet"
    pkt.options = {"timeout": "11"}

    sendoack = state.serverInitial(pkt, "127.0.0.1", 12345)

    assert sendoack is True
    assert context.options["timeout"] == "11"
    assert context.timeout == 11
    assert context.sock.timeout == 11
