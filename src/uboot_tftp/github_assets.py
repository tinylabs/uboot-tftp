"""Helpers for GitHub release manifests and asset downloads."""

from __future__ import annotations

import json
import zlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .ubootops import uboot_download_url
from .ubootterm import uboot_msg


class GithubAsset(dict[str, Any]):
    @property
    def name(self) -> str:
        name = str(self.get("name", "")).strip()
        if not name:
            raise ValueError("asset must include name")
        return name

    @property
    def download_url(self) -> str:
        url = str(self.get("browser_download_url", "")).strip()
        if not url:
            raise ValueError("asset must include browser_download_url")
        return url

    def crc32(self, binary: bytes, *, size: int | None = None) -> int:
        payload = binary
        if size is not None:
            if size < len(binary):
                raise ValueError("size must be at least the binary length")
            payload = binary + (b"\xFF" * (size - len(binary)))
        return zlib.crc32(payload) & 0xFFFFFFFF


class GithubJsonManifest:
    """Download and cache a GitHub API JSON manifest."""

    def __init__(
        self,
        tftp,
        path: str,
        *,
        destination: str | None = None,
        cache: bool = False,
    ) -> None:
        self.tftp = tftp
        self.path = self._normalize_path(path)
        self.url = f"https://api.github.com/repos/{quote(self.path, safe='/')}"
        self.destination = destination or f"github/{self.path}.json"
        self.artifact_key = f"github-json:{self.path}"
        self._manifest: dict[str, Any] | None = None
        if cache:
            self._load_cached_manifest()

    def _load_cached_manifest(self) -> None:
        if not hasattr(self.tftp, "file_exists") or not hasattr(self.tftp, "read_file"):
            return
        if not self.tftp.file_exists(self.destination):
            return
        payload = self.tftp.read_file(self.destination)
        self._manifest = json.loads(payload)

    @staticmethod
    def _normalize_path(path: str) -> str:
        normalized = str(path).strip().strip("/")
        if not normalized:
            raise ValueError("path must not be empty")
        return normalized

    @property
    def manifest(self) -> dict[str, Any]:
        if self._manifest is None:
            raise RuntimeError("manifest has not been loaded yet")
        return self._manifest

    def assets(self) -> list[GithubAsset]:
        assets = self.manifest.get("assets", [])
        if not isinstance(assets, list):
            raise TypeError("manifest assets field must be a list")
        return [GithubAsset(asset) for asset in assets if isinstance(asset, dict)]

    def find(self, *, match: Iterable[str]) -> list[GithubAsset]:
        needles = [str(value) for value in match if str(value)]
        if not needles:
            return [asset for asset in self.assets() if str(asset.get("name", ""))]
        return [
            asset
            for asset in self.assets()
            if (name := str(asset.get("name", ""))) and all(needle in name for needle in needles)
        ]

    async def download_asset(
        self,
        asset: GithubAsset | dict[str, Any],
        *,
        destination: str | None = None,
        cache: bool = False,
    ) -> bytes:
        if not isinstance(asset, GithubAsset):
            asset = GithubAsset(asset)
        filepath = destination or self._asset_destination(asset.name)
        if cache and self.tftp.file_exists(filepath):
            self.tftp.exec_queue([uboot_msg(f"Using cached asset: {filepath}", bold=True)])
            return self.tftp.read_file(filepath)
        await uboot_download_url(
            self.tftp,
            url=asset.download_url,
            filepath=filepath,
            page_url=self.url,
        )
        return self.tftp.read_file(filepath)

    def _asset_destination(self, name: str) -> str:
        return f"{self.path}/{Path(name).name}"

    async def load(self) -> dict[str, Any]:
        if self._manifest is not None:
            self.tftp.exec_queue([uboot_msg(f"Using cached manifest: {self.destination}", bold=True)])
            return self._manifest

        payload = await uboot_download_url(
            self.tftp,
            url=self.url,
            filepath=self.destination,
            page_url=self.url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self._manifest = json.loads(payload)
        return self._manifest
