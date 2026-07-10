import asyncio
import zlib
from types import SimpleNamespace

import pytest

from uboot_tftp.github_assets import GithubAsset, GithubJsonManifest


class FakeTftp:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.acquire_calls = []
        self.exec_calls = []
        self.exec_queue_calls = []
        self._artifact = None
        self._files = {}
        self._existing = set()
        self._queued_scripts = []

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

    def exec_queue(self, script, requires=None):  # noqa: ARG002
        self.exec_queue_calls.append((script, requires))
        self._queued_scripts.extend(script)

    async def exec(self, script, final=False, keys=()):  # noqa: ARG002
        full_script = [*self._queued_scripts, *script]
        self._queued_scripts.clear()
        self.exec_calls.append((full_script, final))
        if self.acquire_calls:
            self._files[self.acquire_calls[0]["destination"]] = self.payload
        if self._artifact is not None:
            self._artifact.state = "done"


def test_github_json_manifest_starts_download_and_loads_json():
    tftp = FakeTftp(
        b'{"name":"latest","assets":[{"name":"openipc-gk7205v300-lite.bin",'
        b'"browser_download_url":"https://example.com/lite.bin"},'
        b'{"name":"openipc-gk7205v300-ultimate.bin",'
        b'"browser_download_url":"https://example.com/ultimate.bin"},'
        b'{"name":"other.bin","browser_download_url":"https://example.com/other.bin"}]}'
    )

    manifest = GithubJsonManifest(tftp, "OpenIPC/firmware/releases/tags/latest")
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
    tftp = FakeTftp(b"{}")
    destination = "github/OpenIPC/firmware/releases/tags/latest.json"
    tftp._existing.add(destination)
    tftp._files[destination] = (
        b'{"name":"latest","assets":[{"name":"cached-openipc-gk7205v300-lite.bin",'
        b'"browser_download_url":"https://example.com/cached-lite.bin"}]}'
    )

    manifest = GithubJsonManifest(
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
    payload = b"firmware-binary"
    tftp = FakeTftp(payload)
    manifest = GithubJsonManifest(tftp, "OpenIPC/firmware/releases/tags/latest")

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


def test_github_json_manifest_download_asset_uses_cached_file_when_enabled():
    payload = b"cached-binary"
    tftp = FakeTftp(b"unused")
    destination = "OpenIPC/firmware/releases/tags/latest/openipc-gk7205v300-lite.bin"
    tftp._existing.add(destination)
    tftp._files[destination] = payload
    manifest = GithubJsonManifest(tftp, "OpenIPC/firmware/releases/tags/latest")

    binary = asyncio.run(
        manifest.download_asset(
            {
                "name": "openipc-gk7205v300-lite.bin",
                "browser_download_url": "https://example.com/lite.bin",
            },
            cache=True,
        )
    )

    assert binary == payload
    assert tftp.acquire_calls == []
    assert tftp.exec_calls == []
    assert len(tftp.exec_queue_calls) == 1
    assert "Using cached asset: OpenIPC/firmware/releases/tags/latest/openipc-gk7205v300-lite.bin" in (
        tftp.exec_queue_calls[0][0][0]
    )


def test_github_asset_crc32_matches_uboot_algorithm():
    asset = GithubAsset(name="firmware.bin")
    payload = b"firmware-binary"

    assert asset.crc32(payload) == (zlib.crc32(payload) & 0xFFFFFFFF)


def test_github_asset_crc32_can_pad_with_erased_flash_bytes():
    asset = GithubAsset(name="firmware.bin")
    payload = b"\x01\x02\x03"
    padded = payload + (b"\xFF" * 5)

    assert asset.crc32(payload, size=8) == (zlib.crc32(padded) & 0xFFFFFFFF)


def test_github_asset_requires_name():
    with pytest.raises(ValueError, match="asset must include name"):
        _ = GithubAsset({}).name


def test_github_asset_requires_download_url():
    with pytest.raises(ValueError, match="asset must include browser_download_url"):
        _ = GithubAsset(name="firmware.bin").download_url
