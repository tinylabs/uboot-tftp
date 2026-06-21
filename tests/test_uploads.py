import pytest

from openipc_tftp.uploads import DiskUploadStore, InMemoryUploadStore, UploadRequest


def test_in_memory_upload_store_captures_upload_and_indexes_by_client_id():
    store = InMemoryUploadStore()
    fileobj = store.open(
        UploadRequest(
            filename="id=cam123/upload/env.txt",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 69),
        )
    )

    fileobj.write(b"payload")
    fileobj.close()

    uploads = store.all()
    assert len(uploads) == 1
    assert uploads[0].size == 7
    assert uploads[0].body == b"payload"
    assert store.by_client_id["cam123"] == uploads


def test_in_memory_upload_store_keeps_unparseable_upload_without_client_id_index():
    store = InMemoryUploadStore()
    fileobj = store.open(
        UploadRequest(
            filename="plain.txt",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 69),
        )
    )

    fileobj.write(b"payload")
    fileobj.close()

    assert len(store.all()) == 1
    assert store.by_client_id == {}


def test_disk_upload_store_persists_upload_under_requested_filename(tmp_path):
    store = DiskUploadStore(tmp_path)
    fileobj = store.open(
        UploadRequest(
            filename="id=cam123/upload/env.txt",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 69),
        )
    )

    fileobj.write(b"payload")
    fileobj.close()

    upload_path = tmp_path / "cam123" / "upload" / "env.txt"
    assert upload_path.read_bytes() == b"payload"
    assert store.all()[0].body == b"payload"
    assert store.all()[0].filename == "id=cam123/upload/env.txt"


def test_disk_upload_store_rejects_path_traversal(tmp_path):
    store = DiskUploadStore(tmp_path)

    with pytest.raises(ValueError, match="unsafe upload filename"):
        store.record(
            UploadRequest(
                filename="../escape.txt",
                peer=("127.0.0.1", 12345),
                server_addr=("127.0.0.1", 69),
            ),
            b"payload",
        )
