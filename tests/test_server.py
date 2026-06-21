from openipc_tftp import CallableContentProvider, ContentResult
from openipc_tftp.server import DynamicContentServer, fileobj_from_result
from openipc_tftp.uploads import InMemoryUploadStore


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


def test_dynamic_content_server_opens_upload_sink(tmp_path):
    upload_store = InMemoryUploadStore()
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
        str(tmp_path / "id=cam123" / "upload" / "env.txt"),
        context,
    )
    upload.write(b"payload")
    upload.close()

    assert context.flock is False
    captured = upload_store.all()
    assert len(captured) == 1
    assert captured[0].filename == "id=cam123/upload/env.txt"
    assert captured[0].body == b"payload"
    assert upload_store.by_client_id["cam123"][0] == captured[0]


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


def test_fileobj_from_result_wraps_bytes():
    fileobj = fileobj_from_result(ContentResult.from_bytes(b"payload"))

    assert fileobj.read() == b"payload"
    assert isinstance(fileobj.fileno(), int)
