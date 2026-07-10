import asyncio
import importlib.util
import io
import tarfile
import zlib
from pathlib import Path
from types import SimpleNamespace

from uboot_tftp.partitions import parse_mtdparts_spec
from uboot_tftp.ubootenv import ubootenv_parse_part


def load_openipc_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "openipc.py"
    spec = importlib.util.spec_from_file_location("openipc_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openipc_load_release_assets_uses_release_uboot_env_for_partition_table(monkeypatch):
    module = load_openipc_module()
    release_env = {
        "mtdpartsnor16m": (
            "setenv mtdparts "
            "sfc:256k(boot),64k(env),3072k(kernel),10240k(rootfs),-(rootfs_data)"
        ),
    }
    assets = [
        {
            "name": "u-boot-gk7205v300.bin",
            "browser_download_url": "https://example.com/u-boot-gk7205v300.bin",
        },
        {
            "name": "kernel-gk7205v300-lite.bin",
            "browser_download_url": "https://example.com/kernel-gk7205v300-lite.bin",
        },
        {
            "name": "rootfs-gk7205v300-lite.bin",
            "browser_download_url": "https://example.com/rootfs-gk7205v300-lite.bin",
        },
    ]
    payloads = {
        "u-boot-gk7205v300.bin": b"uboot",
        "kernel-gk7205v300-lite.bin": b"kernel",
        "rootfs-gk7205v300-lite.bin": b"rootfs",
    }

    class FakeManifest:
        def __init__(self, tftp, path, *, cache=False):  # noqa: ARG002
            self.path = path

        async def load(self):
            return {}

        def find(self, *, match):
            return [
                asset
                for asset in assets
                if all(token in asset["name"] for token in match)
            ]

        async def download_asset(self, asset, *, destination=None, cache=False):  # noqa: ARG002
            return payloads[Path(destination).name]

    monkeypatch.setattr(module, "GithubJsonManifest", FakeManifest)
    monkeypatch.setattr(module, "ubootenv_extract", lambda payload: release_env)

    context = module.OpenIpcInstallContext(
        ident="cam123",
        cmd="install",
        env={"soc": "gk7205v300", "fw": "lite"},
        nor_size=16 * 2**20,
        soc="gk7205v300",
        fw="lite",
        cache=False,
        tag="stable",
    )

    release = asyncio.run(module.openipc_load_release_assets(object(), context))

    assert release.partition_table.range("kernel") == (0x50000, 0x300000)
    assert release.partition_table.range("rootfs") == (0x350000, 0xA00000)
    assert release.manifest.path == "OpenIPC/firmware/releases/tags/stable"


def test_openipc_build_partition_payloads_builds_sized_env_partition():
    module = load_openipc_module()
    context = module.OpenIpcInstallContext(
        ident="cam123",
        cmd="install",
        env={
            "ethaddr": "00:11:22:33:44:55",
            "serverip": "192.168.1.1",
            "soc": "gk7205v300",
            "fw": "lite",
        },
        nor_size=8 * 2**20,
        soc="gk7205v300",
        fw="lite",
        cache=True,
        tag="latest",
    )
    release = module.OpenIpcReleaseAssets(
        manifest=SimpleNamespace(path="OpenIPC/firmware/releases/tags/latest"),
        release_env={
            "bootcmd": "run boot",
            "mtdparts": "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
        },
        partition_table=parse_mtdparts_spec(
            "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
            total_size=8 * 2**20,
        ),
        uboot_asset={"browser_download_url": "https://example.com/u-boot.bin"},
        uboot_payload=b"uboot",
        kernel_asset={"browser_download_url": "https://example.com/kernel.bin"},
        kernel_payload=b"kernel",
        rootfs_asset={"browser_download_url": "https://example.com/rootfs.bin"},
        rootfs_payload=b"rootfs",
    )

    class FakeTftp:
        rambase = "loadaddr"
        is_le = True

    payloads = module.openipc_build_partition_payloads(FakeTftp(), context, release)
    env_payload = next(payload for payload in payloads if payload.name == "env")
    env_data = ubootenv_parse_part(env_payload.payload)

    assert len(env_payload.payload) == 0x10000
    assert env_data["hostname"] == "cam123"
    assert env_data["bootp_vci"] == "uboot.cam123"
    assert env_data["install"] == "cmd=install; run bootstrap"


def test_openipc_build_partition_payloads_uses_tftp_endianness_for_env_crc():
    module = load_openipc_module()
    context = module.OpenIpcInstallContext(
        ident="cam123",
        cmd="install",
        env={
            "ethaddr": "00:11:22:33:44:55",
            "serverip": "192.168.1.1",
            "soc": "gk7205v300",
            "fw": "lite",
        },
        nor_size=8 * 2**20,
        soc="gk7205v300",
        fw="lite",
        cache=True,
        tag="latest",
    )
    release = module.OpenIpcReleaseAssets(
        manifest=SimpleNamespace(path="OpenIPC/firmware/releases/tags/latest"),
        release_env={
            "bootcmd": "run boot",
            "mtdparts": "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
        },
        partition_table=parse_mtdparts_spec(
            "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
            total_size=8 * 2**20,
        ),
        uboot_asset={"browser_download_url": "https://example.com/u-boot.bin"},
        uboot_payload=b"uboot",
        kernel_asset={"browser_download_url": "https://example.com/kernel.bin"},
        kernel_payload=b"kernel",
        rootfs_asset={"browser_download_url": "https://example.com/rootfs.bin"},
        rootfs_payload=b"rootfs",
    )

    class FakeTftp:
        rambase = "loadaddr"
        is_le = False

    payloads = module.openipc_build_partition_payloads(FakeTftp(), context, release)
    env_payload = next(payload for payload in payloads if payload.name == "env")
    payload = env_payload.payload[4:]
    crc = zlib.crc32(payload) & 0xFFFFFFFF

    assert env_payload.payload[:4] == crc.to_bytes(4, "big")


def test_openipc_load_release_assets_uses_context_cache_for_manifest_and_assets(monkeypatch):
    module = load_openipc_module()
    seen = {"manifest_cache": None, "asset_cache": []}

    class FakeManifest:
        def __init__(self, tftp, path, *, cache=False):  # noqa: ARG002
            self.path = path
            seen["manifest_cache"] = cache

        async def load(self):
            return {}

        def find(self, *, match):
            token = match[-1]
            return [
                {
                    "name": f"{token}-gk7205v300.bin",
                    "browser_download_url": f"https://example.com/{token}-gk7205v300.bin",
                }
            ]

        async def download_asset(self, asset, *, destination=None, cache=False):  # noqa: ARG002
            seen["asset_cache"].append(cache)
            return b"payload"

    monkeypatch.setattr(module, "GithubJsonManifest", FakeManifest)
    monkeypatch.setattr(
        module,
        "ubootenv_extract",
        lambda payload: {
            "mtdparts": "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
        },
    )

    context = module.OpenIpcInstallContext(
        ident="cam123",
        cmd="install",
        env={"soc": "gk7205v300", "fw": "lite"},
        nor_size=8 * 2**20,
        soc="gk7205v300",
        fw="lite",
        cache=False,
        tag="stable",
    )

    asyncio.run(module.openipc_load_release_assets(object(), context))

    assert seen["manifest_cache"] is False
    assert seen["asset_cache"] == [False, False, False]


def test_openipc_load_release_assets_can_extract_kernel_and_rootfs_from_tgz(monkeypatch):
    module = load_openipc_module()
    release_env = {
        "mtdparts": "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
    }
    bundle_name = "openipc.gk7205v300-nor-lite.tgz"
    bundle_payload_io = io.BytesIO()
    with tarfile.open(fileobj=bundle_payload_io, mode="w:gz") as tar:
        kernel_payload = b"kernel-image"
        kernel_info = tarfile.TarInfo(name="uImage.gk7205v300")
        kernel_info.size = len(kernel_payload)
        tar.addfile(kernel_info, io.BytesIO(kernel_payload))

        rootfs_payload = b"rootfs-image"
        rootfs_info = tarfile.TarInfo(name="rootfs.squashfs")
        rootfs_info.size = len(rootfs_payload)
        tar.addfile(rootfs_info, io.BytesIO(rootfs_payload))
    bundle_payload = bundle_payload_io.getvalue()

    assets = [
        {
            "name": "u-boot-gk7205v300.bin",
            "browser_download_url": "https://example.com/u-boot-gk7205v300.bin",
        },
        {
            "name": bundle_name,
            "browser_download_url": f"https://example.com/{bundle_name}",
        },
    ]
    payloads = {
        "u-boot-gk7205v300.bin": b"uboot",
        bundle_name: bundle_payload,
    }

    class FakeManifest:
        def __init__(self, tftp, path, *, cache=False):  # noqa: ARG002
            self.path = path

        async def load(self):
            return {}

        def find(self, *, match):
            return [
                asset
                for asset in assets
                if all(token in asset["name"] for token in match)
            ]

        async def download_asset(self, asset, *, destination=None, cache=False):  # noqa: ARG002
            return payloads[Path(destination).name]

    monkeypatch.setattr(module, "GithubJsonManifest", FakeManifest)
    monkeypatch.setattr(module, "ubootenv_extract", lambda payload: release_env)

    context = module.OpenIpcInstallContext(
        ident="cam123",
        cmd="install",
        env={"soc": "gk7205v300", "fw": "lite"},
        nor_size=8 * 2**20,
        soc="gk7205v300",
        fw="lite",
        cache=True,
        tag="latest",
    )

    release = asyncio.run(module.openipc_load_release_assets(object(), context))

    assert release.kernel_payload == b"kernel-image"
    assert release.rootfs_payload == b"rootfs-image"
    assert release.kernel_asset["name"] == "uImage.gk7205v300"
    assert release.rootfs_asset["name"] == "rootfs.squashfs"


def test_openipc_build_partition_payloads_prefers_extracted_member_names_for_sources():
    module = load_openipc_module()
    context = module.OpenIpcInstallContext(
        ident="cam123",
        cmd="install",
        env={
            "ethaddr": "00:11:22:33:44:55",
            "serverip": "192.168.1.1",
            "soc": "gk7205v300",
            "fw": "lite",
        },
        nor_size=8 * 2**20,
        soc="gk7205v300",
        fw="lite",
        cache=True,
        tag="latest",
    )
    release = module.OpenIpcReleaseAssets(
        manifest=SimpleNamespace(path="OpenIPC/firmware/releases/tags/latest"),
        release_env={
            "bootcmd": "run boot",
            "mtdparts": "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
        },
        partition_table=parse_mtdparts_spec(
            "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
            total_size=8 * 2**20,
        ),
        uboot_asset={"browser_download_url": "https://example.com/u-boot.bin"},
        uboot_payload=b"uboot",
        kernel_asset={
            "name": "uImage.gk7205v300",
            "browser_download_url": "https://example.com/openipc.gk7205v300-nor-lite.tgz",
        },
        kernel_payload=b"kernel",
        rootfs_asset={
            "name": "rootfs.squashfs",
            "browser_download_url": "https://example.com/openipc.gk7205v300-nor-lite.tgz",
        },
        rootfs_payload=b"rootfs",
    )

    class FakeTftp:
        rambase = "loadaddr"
        is_le = True

    payloads = module.openipc_build_partition_payloads(FakeTftp(), context, release)

    kernel_payload = next(payload for payload in payloads if payload.name == "kernel")
    rootfs_payload = next(payload for payload in payloads if payload.name == "rootfs")
    assert kernel_payload.source == "uImage.gk7205v300"
    assert rootfs_payload.source == "rootfs.squashfs"
