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


def test_openipc_asset_selection_uses_exact_soc_for_uboot_and_t_family_for_bundle():
    module = load_openipc_module()
    assets = [
        {"name": "u-boot-t30a-nor.bin"},
        {"name": "u-boot-t30a1-nor.bin"},
        {"name": "u-boot-t30l-nor.bin"},
        {"name": "u-boot-t30n-nor.bin"},
        {"name": "u-boot-t30x-nor.bin"},
        {"name": "boot-hi3516cv608-nor.bin"},
        {"name": "boot-hi3516cv610-00g-nor.bin"},
        {"name": "openipc.hi3516cv6xx-nor-ultimate.tgz"},
        {"name": "openipc.t30-nor-lite.tgz"},
    ]

    class Manifest:
        def assets(self):
            return [module.GithubAsset(asset) for asset in assets]

        def find(self, *, match):
            return [
                module.GithubAsset(asset)
                for asset in assets
                if all(token in asset["name"] for token in match)
            ]

    manifest = Manifest()

    assert module.openipc_find_release_asset(
        manifest, soc="t30a", fw="lite", partition="uboot"
    )["name"] == "u-boot-t30a-nor.bin"
    assert module.openipc_find_release_asset(
        manifest, soc="t30a1", fw="lite", partition="uboot"
    )["name"] == "u-boot-t30a1-nor.bin"
    assert module.openipc_find_release_asset(
        manifest, soc="t30x", fw="lite", partition="uboot"
    )["name"] == "u-boot-t30x-nor.bin"
    assert module.openipc_find_release_asset(
        manifest, soc="hi3516cv610-00g", fw="lite", partition="uboot"
    )["name"] == "boot-hi3516cv610-00g-nor.bin"
    assert module.openipc_find_release_asset(
        manifest, soc="hi3516cv610-00g", fw="ultimate", partition="firmware_bundle"
    )["name"] == "openipc.hi3516cv6xx-nor-ultimate.tgz"
    assert module.openipc_find_release_asset(
        manifest, soc="t30a1", fw="lite", partition="firmware_bundle"
    )["name"] == "openipc.t30-nor-lite.tgz"


def test_openipc_asset_selection_prefers_universal_uboot_over_nand():
    module = load_openipc_module()
    assets = [
        {"name": "u-boot-hi3516ev300-nand.bin"},
        {"name": "u-boot-hi3516ev300-universal.bin"},
    ]

    class Manifest:
        def find(self, *, match):
            return [
                module.GithubAsset(asset)
                for asset in assets
                if all(token in asset["name"] for token in match)
            ]

    asset = module.openipc_find_release_asset(
        Manifest(), soc="hi3516ev300", fw="lite", partition="uboot"
    )

    assert asset["name"] == "u-boot-hi3516ev300-universal.bin"


def test_openipc_erase_overlay_erases_resolved_partition():
    module = load_openipc_module()
    table = parse_mtdparts_spec(
        "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
        total_size=16 * 2**20,
    )

    assert module._openipc_overlay_partition(table, {}) is None
    assert module._openipc_overlay_partition(table, {"erase_overlay": "0"}) is None
    partition = module._openipc_overlay_partition(
        table,
        {"erase_overlay": "1"},
    )
    assert partition is not None
    assert partition.name == "rootfs_data"
    assert partition.offset == 0x750000
    assert partition.size == 0x8B0000

    class FakeTftp:
        def __init__(self):
            self.queued = []

        def exec_queue(self, script, *, requires=()):
            self.queued.append((script, requires))

    tftp = FakeTftp()

    asyncio.run(module.openipc_erase_overlay(tftp, partition))

    script, requires = tftp.queued[0]
    assert "Erasing overlay (rootfs_data)" in script[0]
    assert script[1].splitlines() == [
        "sf probe 0",
        "sf erase 0x750000 0x8b0000",
    ]
    assert requires == ["sf probe", "sf erase"]


def test_openipc_asset_destination_is_shared_by_soc_variants():
    module = load_openipc_module()
    manifest = SimpleNamespace(path="OpenIPC/firmware/releases/tags/latest")
    asset = {
        "browser_download_url": (
            "https://github.com/OpenIPC/firmware/releases/download/nightly/"
            "openipc.t30-nor-lite.tgz"
        )
    }

    t30a_path = module._asset_destination(manifest, asset, "t30a")
    t30a1_path = module._asset_destination(manifest, asset, "t30a1")

    assert t30a_path == t30a1_path
    assert t30a_path.endswith("/assets/openipc.t30-nor-lite.tgz")


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
    monkeypatch.setattr(module, "extract_default_env_from_uboot", lambda payload: release_env)

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


def test_openipc_build_partition_payloads_sets_resolved_mtdparts_in_bootargs():
    module = load_openipc_module()
    context = module.OpenIpcInstallContext(
        ident="cam123",
        cmd="install",
        env={"ethaddr": "00:11:22:33:44:55", "soc": "gk7205v300", "fw": "lite"},
        nor_size=8 * 2**20,
        soc="gk7205v300",
        fw="lite",
        cache=True,
        tag="latest",
    )
    spec = "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)"
    release = module.OpenIpcReleaseAssets(
        manifest=SimpleNamespace(path="OpenIPC/firmware/releases/tags/latest"),
        release_env={"bootargs": f"console=ttyS0 mtdparts={spec} LX_MEM=0x200000"},
        partition_table=parse_mtdparts_spec(spec, total_size=8 * 2**20),
        uboot_asset={"browser_download_url": "https://example.com/u-boot.bin"},
        uboot_payload=b"uboot",
        kernel_asset={"browser_download_url": "https://example.com/kernel.bin"},
        kernel_payload=b"kernel",
        rootfs_asset={"browser_download_url": "https://example.com/rootfs.bin"},
        rootfs_payload=b"rootfs",
        mtdparts_spec=spec,
    )

    class FakeTftp:
        rambase = "loadaddr"
        is_le = True

    payloads = module.openipc_build_partition_payloads(FakeTftp(), context, release)
    env_data = ubootenv_parse_part(next(item for item in payloads if item.name == "env").payload)

    assert env_data["_mtdparts"] == spec
    assert env_data["bootargs"] == "console=ttyS0 mtdparts=${_mtdparts} LX_MEM=0x200000"


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
        "extract_default_env_from_uboot",
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
    monkeypatch.setattr(module, "extract_default_env_from_uboot", lambda payload: release_env)

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


def test_openipc_load_release_assets_falls_back_to_latest_for_missing_tagged_uboot(monkeypatch):
    module = load_openipc_module()
    release_env = {
        "mtdparts": "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
    }
    assets_by_path = {
        "OpenIPC/firmware/releases/tags/stable": [
            {
                "name": "kernel-gk7205v300-lite.bin",
                "browser_download_url": "https://example.com/stable-kernel-gk7205v300-lite.bin",
            },
            {
                "name": "rootfs-gk7205v300-lite.bin",
                "browser_download_url": "https://example.com/stable-rootfs-gk7205v300-lite.bin",
            },
        ],
        "OpenIPC/firmware/releases/tags/latest": [
            {
                "name": "u-boot-gk7205v300.bin",
                "browser_download_url": "https://example.com/latest-u-boot-gk7205v300.bin",
            },
        ],
    }
    payloads = {
        "latest-u-boot-gk7205v300.bin": b"uboot",
        "stable-kernel-gk7205v300-lite.bin": b"kernel",
        "stable-rootfs-gk7205v300-lite.bin": b"rootfs",
    }
    seen_paths = []

    class FakeManifest:
        def __init__(self, tftp, path, *, cache=False):  # noqa: ARG002
            self.path = path

        async def load(self):
            seen_paths.append(self.path)
            return {}

        def find(self, *, match):
            return [
                asset
                for asset in assets_by_path[self.path]
                if all(token in asset["name"] for token in match)
            ]

        async def download_asset(self, asset, *, destination=None, cache=False):  # noqa: ARG002
            return payloads[Path(destination).name]

    monkeypatch.setattr(module, "GithubJsonManifest", FakeManifest)
    monkeypatch.setattr(module, "extract_default_env_from_uboot", lambda payload: release_env)

    context = module.OpenIpcInstallContext(
        ident="cam123",
        cmd="install",
        env={"soc": "gk7205v300", "fw": "lite"},
        nor_size=8 * 2**20,
        soc="gk7205v300",
        fw="lite",
        cache=True,
        tag="stable",
    )

    release = asyncio.run(module.openipc_load_release_assets(object(), context))

    assert release.manifest.path == "OpenIPC/firmware/releases/tags/stable"
    assert release.uboot_asset["browser_download_url"] == "https://example.com/latest-u-boot-gk7205v300.bin"
    assert release.kernel_asset["browser_download_url"] == "https://example.com/stable-kernel-gk7205v300-lite.bin"
    assert release.rootfs_asset["browser_download_url"] == "https://example.com/stable-rootfs-gk7205v300-lite.bin"
    assert seen_paths == [
        "OpenIPC/firmware/releases/tags/stable",
        "OpenIPC/firmware/releases/tags/latest",
    ]


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
