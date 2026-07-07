import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace


def load_openipc_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "openipc.py"
    spec = importlib.util.spec_from_file_location("openipc_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTftp:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.acquire_calls = []
        self.exec_calls = []
        self._artifact = None
        self._files = {}
        self._existing = set()

    def acquire_download(self, **kwargs):
        self.acquire_calls.append(kwargs)
        self._artifact = SimpleNamespace(
            artifact_key=kwargs["artifact_key"],
            state="pending",
            bytes_done=0,
            bytes_total=None,
            error=None,
        )
        return self._artifact

    def get_download(self, artifact_key):
        if self._artifact is None or artifact_key != self._artifact.artifact_key:
            return None
        return self._artifact

    def read_file(self, filename):
        return self._files[filename]

    def file_exists(self, filename):
        return filename in self._existing

    async def exec(self, script, final=False, keys=()):  # noqa: ARG002
        self.exec_calls.append((script, final))
        self._files[self.acquire_calls[0]["destination"]] = self.payload
        self._artifact.state = "done"


def test_github_json_manifest_starts_download_and_loads_json():
    module = load_openipc_module()
    tftp = FakeTftp(
        b'{"name":"latest","assets":[{"name":"openipc-gk7205v300-lite.bin",'
        b'"browser_download_url":"https://example.com/lite.bin"},'
        b'{"name":"openipc-gk7205v300-ultimate.bin",'
        b'"browser_download_url":"https://example.com/ultimate.bin"},'
        b'{"name":"other.bin","browser_download_url":"https://example.com/other.bin"}]}'
    )

    manifest = module.GithubJsonManifest(tftp, "OpenIPC/firmware/releases/tags/latest")
    data = asyncio.run(manifest.load())

    assert manifest.path == "OpenIPC/firmware/releases/tags/latest"
    assert manifest.url == "https://api.github.com/repos/OpenIPC/firmware/releases/tags/latest"
    assert manifest.destination == "github/OpenIPC/firmware/releases/tags/latest.json"
    assert data["name"] == "latest"
    assert data["assets"][0]["name"] == "openipc-gk7205v300-lite.bin"
    assert manifest.find(match=["gk7205v300", "lite"]) == [
        {
            "name": "openipc-gk7205v300-lite.bin",
            "browser_download_url": "https://example.com/lite.bin",
        }
    ]
    assert manifest.find(match=[]) == [
        {
            "name": "openipc-gk7205v300-lite.bin",
            "browser_download_url": "https://example.com/lite.bin",
        },
        {
            "name": "openipc-gk7205v300-ultimate.bin",
            "browser_download_url": "https://example.com/ultimate.bin",
        },
        {
            "name": "other.bin",
            "browser_download_url": "https://example.com/other.bin",
        },
    ]
    assert len(tftp.acquire_calls) == 1
    assert tftp.acquire_calls[0]["artifact_key"] == (
        "https://api.github.com/repos/OpenIPC/firmware/releases/tags/latest"
    )
    assert tftp.acquire_calls[0]["destination"] == "github/OpenIPC/firmware/releases/tags/latest.json"
    assert tftp.acquire_calls[0]["url"] == "https://api.github.com/repos/OpenIPC/firmware/releases/tags/latest"
    assert tftp.acquire_calls[0]["page_url"] == "https://api.github.com/repos/OpenIPC/firmware/releases/tags/latest"
    assert tftp.acquire_calls[0]["headers"]["Accept"] == "application/vnd.github+json"
    assert tftp.exec_calls


def test_github_json_manifest_uses_cached_file_in_constructor():
    module = load_openipc_module()
    tftp = FakeTftp(b"{}")
    destination = "github/OpenIPC/firmware/releases/tags/latest.json"
    tftp._existing.add(destination)
    tftp._files[destination] = (
        b'{"name":"latest","assets":[{"name":"cached-openipc-gk7205v300-lite.bin",'
        b'"browser_download_url":"https://example.com/cached-lite.bin"}]}'
    )

    manifest = module.GithubJsonManifest(
        tftp,
        "OpenIPC/firmware/releases/tags/latest",
        cache=True,
    )

    assert manifest.manifest["name"] == "latest"
    assert manifest.find(match=["cached", "lite"]) == [
        {
            "name": "cached-openipc-gk7205v300-lite.bin",
            "browser_download_url": "https://example.com/cached-lite.bin",
        }
    ]
    assert tftp.acquire_calls == []
    assert tftp.exec_calls == []


def test_github_json_manifest_download_asset_downloads_and_reads_binary():
    module = load_openipc_module()
    payload = b"firmware-binary"
    tftp = FakeTftp(payload)
    manifest = module.GithubJsonManifest(tftp, "OpenIPC/firmware/releases/tags/latest")

    binary = asyncio.run(
        manifest.download_asset(
            {
                "name": "openipc-gk7205v300-lite.bin",
                "browser_download_url": "https://example.com/lite.bin",
            }
        )
    )

    assert binary == payload
    assert len(tftp.acquire_calls) == 1
    assert tftp.acquire_calls[0]["url"] == "https://example.com/lite.bin"
    assert tftp.acquire_calls[0]["destination"] == (
        "OpenIPC/firmware/releases/tags/latest/openipc-gk7205v300-lite.bin"
    )
    assert tftp.read_file(tftp.acquire_calls[0]["destination"]) == payload
