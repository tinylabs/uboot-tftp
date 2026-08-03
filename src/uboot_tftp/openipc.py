#!/usr/bin/env python3
"""
Example handler module for uboot-tftp.
Implements installing openipc on ip cameras
"""

from __future__ import annotations

import io
import random
import re
import tarfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from uboot_tftp.flashplan import PartitionPayload, PartitionUpdate, build_partition_update_plan
from uboot_tftp.github_assets import GithubAsset, GithubJsonManifest
from uboot_tftp.partitions import (
    PartitionEntry,
    PartitionTable,
    replace_mtdparts_spec,
    resolve_env_references,
)
from uboot_tftp.ubootscript import *
from uboot_tftp.ubootops import *
from uboot_tftp.ubootterm import *
from uboot_tftp.ubootenv import *
from uboot_tftp.tools import *

OPENIPC_RELEASE_PATH_PREFIX = "OpenIPC/firmware/releases/tags"
OPENIPC_FIRST_BOOT_VAR = "openipc_firstboot"
FLASH_SNAPSHOT_RAM_OFFSET = 16 * 2**20
FLASH_STAGE_RAM_OFFSET = 1 * 2**20


def openipc_partition_table(
    env: dict[str, str],
    *,
    flash_size: int | None = None,
    flash_type: str | None = None,
    key: str | None = None,
    payload_sizes: dict[str, int] | None = None,
) -> PartitionTable:
    table, _ = _openipc_partition_layout(
        env,
        flash_size=flash_size,
        flash_type=flash_type,
        key=key,
        payload_sizes=payload_sizes,
    )
    return table


def _openipc_partition_layout(
    env: dict[str, str],
    *,
    flash_size: int | None,
    flash_type: str | None,
    key: str | None,
    payload_sizes: dict[str, int] | None,
) -> tuple[PartitionTable, str]:
    valid: list[tuple[int, PartitionTable, str]] = []
    resolved_layouts = 0
    for candidate in _openipc_mtdparts_keys(
        env,
        flash_size=flash_size,
        flash_type=flash_type,
        key=key,
    ):
        value = env.get(candidate)
        if value is None:
            continue
        spec = extract_mtdparts_spec(value)
        if spec is None:
            # An indirect value such as ``mtdparts=${layout}`` cannot be
            # recognized until expanded.  Prefer extracting first, though:
            # bootargs often has unrelated unresolved variables after an
            # otherwise valid table (e.g. memory tuning parameters).
            try:
                spec = extract_mtdparts_spec(resolve_env_references(value, env))
            except ValueError:
                continue
        if spec is None:
            continue
        try:
            spec = resolve_env_references(spec, env)
            table = parse_mtdparts_spec(spec, total_size=flash_size)
            _validate_openipc_partition_table(table)
        except (KeyError, ValueError):
            continue
        resolved_layouts += 1
        if payload_sizes is None:
            return table, spec
        if _partition_payloads_fit(table, payload_sizes):
            valid.append((len(valid), table, spec))
    if valid:
        _, table, spec = min(
            valid,
            key=lambda item: (_install_layout_end(item[1]), item[0]),
        )
        return table, spec
    if payload_sizes is not None and resolved_layouts:
        sizes = ", ".join(
            f"{name}={size//2**10}kB" for name, size in sorted(payload_sizes.items())
        )
        raise ValueError(
            "Release assets do not fit mtdparts layout "
            f"({sizes}) "
            f"flash={flash_size//2**10}kB"
        )
    raise ValueError("Unable to find an OpenIPC mtdparts specification in environment")


def _openipc_mtdparts_keys(
    env: dict[str, str],
    *,
    flash_size: int | None,
    flash_type: str | None,
    key: str | None,
) -> list[str]:
    if key is not None:
        return [key]

    keys: list[str] = []
    if flash_type is not None and flash_size is not None:
        size_mb = flash_size // 2**20
        kind = flash_type.strip().lower()
        if kind == "nor":
            keys.append(f"mtdpartsnor{size_mb}m")
        elif kind == "nand":
            keys.extend(["mtdpartsnand", "mtdpartsubi"])

    keys.append("mtdparts")
    keys.extend(
        sorted(name for name in env if name.startswith("mtdpartsnor") and name not in keys)
    )
    keys.extend(sorted(name for name in env if "mtdparts" in name and name not in keys))
    # A table may be embedded in bootargs or another arbitrary default-env
    # variable rather than exposed through an mtdparts-named variable.
    keys.extend(sorted(name for name in env if name not in keys))
    return keys


def _validate_openipc_partition_table(table: PartitionTable) -> None:
    """Ensure a resolved release table is safe and usable for installation."""
    if table.total_size is None:
        raise ValueError("flash capacity is required to validate mtdparts")

    entries = table.resolved_entries()
    previous_end = 0
    for entry in entries:
        assert entry.size is not None
        if entry.size <= 0:
            raise ValueError(f"partition {entry.name!r} has a non-positive size")
        if entry.offset < previous_end:
            raise ValueError(f"partition {entry.name!r} overlaps a previous partition")
        previous_end = entry.offset + entry.size

    _require_partition(table, "uboot", "boot")
    _require_partition(table, "env")
    _require_partition(table, "kernel")
    _require_partition(table, "rootfs")


def _partition_payloads_fit(table: PartitionTable, payload_sizes: dict[str, int]) -> bool:
    for name, payload_size in payload_sizes.items():
        names = ("uboot", "boot") if name == "uboot" else (name,)
        try:
            entry = _require_partition(table, *names)
        except ValueError:
            return False
        if payload_size > entry.size:
            return False
    return True


def _install_layout_end(table: PartitionTable) -> int:
    return max(
        entry.offset + entry.size
        for entry in (
            _require_partition(table, "uboot", "boot"),
            _require_partition(table, "env"),
            _require_partition(table, "kernel"),
            _require_partition(table, "rootfs"),
        )
    )


class OpenIpcInstallContext:
    def __init__(
        self,
        *,
        ident: str,
        cmd: str,
        env: dict[str, str],
        nor_size: int,
        soc: str,
        fw: str,
        cache: bool,
        tag: str,
    ) -> None:
        self.ident = ident
        self.cmd = cmd
        self.env = env
        self.nor_size = nor_size
        self.soc = soc
        self.fw = fw
        self.cache = cache
        self.tag = tag


class OpenIpcReleaseAssets:
    def __init__(
        self,
        *,
        manifest: GithubJsonManifest,
        release_env: dict[str, str],
        partition_table: PartitionTable,
        uboot_asset: GithubAsset,
        uboot_payload: bytes,
        kernel_asset: GithubAsset,
        kernel_payload: bytes,
        rootfs_asset: GithubAsset,
        rootfs_payload: bytes,
        mtdparts_spec: str | None = None,
    ) -> None:
        self.manifest = manifest
        self.release_env = release_env
        self.partition_table = partition_table
        self.uboot_asset = uboot_asset
        self.uboot_payload = uboot_payload
        self.kernel_asset = kernel_asset
        self.kernel_payload = kernel_payload
        self.rootfs_asset = rootfs_asset
        self.rootfs_payload = rootfs_payload
        self.mtdparts_spec = mtdparts_spec

def build_runcmd(cmd: str, args: str=''):
    parts = [f"cmd={cmd}"]
    if args:
        parts.append(f"args={args}")
    parts.append("run session")
    return "; ".join(parts)

def gen_mac (mac: str) -> str:
    if mac in ('00:00:23:34:45:66', '00:00:00:00:00:00', '02:00:11:22:33:44'):
        mac_bytes = [0x02] + [random.randint(0x00, 0xFF) for _ in range(5)]
        mac = ":".join(f"{b:02x}" for b in mac_bytes)
    return mac

def _trunc(s: str, max_len: int) -> str:
    if len(s) > max_len or '$' in s:
        return '...'
    return s

def openipc_patch_env(tftp, ident: str, old_env: dict[str,str], new_env: dict[str,str]):

    # Patch bootargs and _mtdparts
    bootargs = new_env.get("bootargs")
    if bootargs is None:
        raise ValueError("release environment has no bootargs to update mtdparts")
    updated_bootargs = replace_mtdparts_spec(bootargs, "${_mtdparts}")
    if updated_bootargs is None:
        raise ValueError("release bootargs has no mtdparts specification to update")
    bootcmd = new_env.get("bootcmd")
    if bootcmd is None:
        raise ValueError("release environment has no bootcmd to update")
    first_boot = OPENIPC_FIRST_BOOT_VAR
    updated_bootcmd = (
        f'if test "${{{first_boot}}}" = "1"; then '
        f'then setenv {first_boot}; saveenv; fi; '
        f"{bootcmd}"
    )

    overwrite  = {
        'ethaddr'        : gen_mac (old_env.get('ethaddr', '00:00:00:00:00:00')),
        'hostname'       : ident,
        'openipc_update' : build_runcmd ('openipc_update', 'cache=0/fw=${fw}/soc=${soc_part}/tag=${tag}'),
        'tag'            : old_env.get ('tag', 'latest'),
        'fw'             : old_env.get ('fw', 'lite'),
        'bootargs'       : updated_bootargs,
        'bootcmd'        : updated_bootcmd,
        first_boot       : '1',
        '_mtdparts'      : old_env.get ('mtdparts_spec'),
    }
    merge_keys = [
        'ipaddr', 'netmask', 'gatewayip', 'dnsip', 'serverip', 'board', 'soc_part',
        *BUILTIN_VARS
    ]

    # Make sure loadaddr is set for sandbox
    if old_env.get('board', '') == 'sandbox':
        overwrite['baseaddr'] = old_env['loadaddr']
        overwrite['loadaddr'] = old_env['loadaddr']

    # Add new entries + merge old => new env
    new_env.update({k: overwrite[k] for k in overwrite.keys()})
    new_env.update({k: old_env[k] for k in merge_keys if k in old_env})

    msgs = []
    for k, v in overwrite.items():
        msgs += [uboot_msg(f"+  {k:<10} = '{_trunc(v, 30)}'")]
    for k, v in {key: new_env[key] for key in merge_keys if key in old_env}.items():
        msgs += [uboot_msg(f">  {k:<10} = '{_trunc(v, 30)}'")]
    return msgs

def openipc_verify_args (tftp, ident: str, cmd: str,
                         env: dict[str, str]) -> list:
    errors = []
    fw = env.get("fw")
    if not env.get('soc'):
        errors.append ("Must pass soc=<name>")
    if fw not in ('lite', 'ultimate', 'neo'):
        errors.append (f"fw={fw} - Only fw=lite|ultimate supported")
    if errors:
        errors.append (f"ie: {tftp.cmdtftp} {tftp.rambase} "
                     f"{tftp.server_ip}:id={ident}/{cmd}/soc=gk7205v300/fw=lite/tag=latest; "
                     f"source {tftp.rambase}")
    return errors


async def openipc_collect_install_context(
    tftp,
    ident: str,
    cmd: str,
    tftp_env: dict[str, str],
) -> OpenIpcInstallContext:
    cenv = await tftp.fetch_env(
        upload_script=[
            uboot_msg("Fetching current uboot environment... ", nl=False, bold=True),
        ]
    )
    tftp.exec_queue([uboot_msg("OK")])

    # Bootstrap if not already done
    if not all(key in cenv for key in BUILTIN_VARS):
        await builtin_bootstrap(tftp, ident, {**cenv, 'verbose' : '0'})
        cenv.update(builtin_dict(tftp, ident, cenv))

    keys = ["nor_size", "fw", "soc", "cache", "tag"]
    cenv.update({k: tftp_env[k] for k in keys if k in tftp_env})
    cenv.setdefault("fw", "lite")
    cenv.setdefault("nor_size", None)
    cenv.setdefault("cache", "1")
    cenv.setdefault("tag", "latest")
    errors = openipc_verify_args(tftp, ident, cmd, cenv)
    if errors:
        raise ValueError("\n".join(errors))

    # Set soc_part to the passed soc arg. This may be a subset of the
    # environment soc which is really soc_family
    cenv.setdefault("soc_part", tftp_env["soc"])

    nor_size = await uboot_nor_probe(
        tftp,
        max_size=tftp_env.get("nor_size", None),
        pre_cmds=[uboot_msg("Probing NOR flash... ", nl=False, bold=True)],
        post_cmds=[uboot_msg("OK")],
    )

    if nor_size == 0:
        raise ValueError("NOR flash not detected! Aborting...")

    cache = _parse_cache_flag(cenv["cache"])
    tag = str(cenv["tag"]).strip()
    if not tag:
        raise ValueError("tag must not be empty")

    return OpenIpcInstallContext(
        ident=ident,
        cmd=cmd,
        env=cenv,
        nor_size=nor_size,
        soc=cenv["soc"],
        fw=cenv["fw"],
        cache=cache,
        tag=tag,
    )


def _parse_url_filename(url: str) -> str:
    path = urlparse(url).path
    name = Path(path).name.strip()
    if not name:
        raise ValueError(f"unable to determine filename from URL: {url}")
    return name


def _openipc_release_path(tag: str) -> str:
    return f"{OPENIPC_RELEASE_PATH_PREFIX}/{tag}"


def _parse_cache_flag(value: str) -> bool:
    text = str(value).strip()
    if text == "1":
        return True
    if text == "0":
        return False
    raise ValueError(f"cache must be 0 or 1, got: {value!r}")


def _asset_destination(manifest: GithubJsonManifest, asset: GithubAsset, soc: str) -> str:
    url = str(asset.get("browser_download_url", "")).strip()
    # Release assets can be shared by several SoC variants (for example the
    # t30 kernel/rootfs bundle).  Cache by the release asset name, not the
    # requested target, so one download URL always maps to one local path.
    return f"{manifest.path}/assets/{_parse_url_filename(url)}"


def _asset_match_groups(soc: str, fw: str, partition: str) -> list[list[str]]:
    if partition == "uboot":
        return []
    if partition == "firmware_bundle":
        groups = [[soc, "nor", fw, ".tgz"], [soc, "nor", fw, ".tar.gz"]]
        family = _firmware_soc_family(soc)
        if family != soc:
            groups.extend(
                [[family, "nor", fw, ".tgz"], [family, "nor", fw, ".tar.gz"]]
            )
        return groups
    return [[soc, fw, partition], [soc, partition]]


def _firmware_soc_family(soc: str) -> str:
    """Return the shared Ingenic T-series firmware family, when applicable."""
    match = re.fullmatch(r"(t\d+)[a-z]\d*", soc.strip().lower())
    return match.group(1) if match is not None else soc


def _find_exact_uboot_asset(
    manifest: GithubJsonManifest,
    soc: str,
    *,
    prefix: str = "u-boot",
) -> GithubAsset | None:
    target = soc.strip()
    if not target:
        return None
    pattern = re.compile(
        rf"^{re.escape(prefix)}[-_.]{re.escape(target)}(?=$|[-_.])",
        flags=re.IGNORECASE,
    )
    matches = [
        asset
        for asset in manifest.find(match=[prefix])
        if pattern.search(str(asset.get("name", ""))) is not None
    ]
    universal = [
        asset
        for asset in matches
        if re.search(
            r"(?:^|[-_.])universal(?=$|[-_.])",
            str(asset.get("name", "")),
            flags=re.IGNORECASE,
        )
    ]
    if len(universal) == 1:
        return universal[0]

    # NAND installation is not supported, so a NAND-only image must not be
    # selected as a fallback for the NOR install flow.
    supported = [
        asset
        for asset in matches
        if re.search(
            r"(?:^|[-_.])nand(?=$|[-_.])",
            str(asset.get("name", "")),
            flags=re.IGNORECASE,
        )
        is None
    ]
    return supported[0] if len(supported) == 1 else None


def _find_wildcard_firmware_bundle(
    manifest: GithubJsonManifest,
    soc: str,
    fw: str,
) -> GithubAsset | None:
    """Find a shared bundle whose ``x`` characters are SoC wildcards."""
    name_pattern = re.compile(
        rf"^openipc[.-](?P<target>[a-z0-9x]+)-nor-{re.escape(fw)}"
        rf"\.(?:tgz|tar\.gz)$",
        flags=re.IGNORECASE,
    )
    matches: list[GithubAsset] = []
    for asset in manifest.find(match=["openipc", "nor", fw]):
        match = name_pattern.fullmatch(str(asset.get("name", "")))
        if match is None or "x" not in match.group("target").lower():
            continue
        target_pattern = re.escape(match.group("target")).replace("x", "[a-z0-9]")
        if re.match(rf"^{target_pattern}(?:[-_.]|$)", soc, flags=re.IGNORECASE):
            matches.append(asset)
    return matches[0] if len(matches) == 1 else None


def openipc_find_release_asset(
    manifest: GithubJsonManifest,
    *,
    soc: str,
    fw: str,
    partition: str,
) -> GithubAsset:
    if partition == "uboot":
        asset = _find_exact_uboot_asset(manifest, soc)
        if asset is None:
            asset = _find_exact_uboot_asset(manifest, soc, prefix="boot")
        if asset is not None:
            return asset
        raise ValueError(f"unable to resolve a unique uboot asset for soc={soc}")
    for needles in _asset_match_groups(soc, fw, partition):
        matches = manifest.find(match=needles)
        if len(matches) == 1:
            return matches[0]
    if partition == "firmware_bundle":
        asset = _find_wildcard_firmware_bundle(manifest, soc, fw)
        if asset is not None:
            return asset
    raise ValueError(
        f"unable to resolve a unique {partition} asset for soc={soc} fw={fw}"
    )


def _find_release_asset_optional(
    manifest: GithubJsonManifest,
    *,
    soc: str,
    fw: str,
    partition: str,
) -> GithubAsset | None:
    try:
        return openipc_find_release_asset(
            manifest,
            soc=soc,
            fw=fw,
            partition=partition,
        )
    except ValueError:
        return None


async def _load_release_manifest(
    tftp,
    *,
    tag: str,
    cache: bool,
) -> GithubJsonManifest:
    manifest = GithubJsonManifest(
        tftp,
        path=_openipc_release_path(tag),
        cache=cache,
    )
    await manifest.load()
    return manifest


def _extract_tar_member(
    archive: bytes,
    *,
    kind: str,
) -> tuple[str, bytes]:
    aliases = {
        "kernel": ("kernel", "uimage", "image", "zimage"),
        "rootfs": ("rootfs", "squashfs", "ubi", "ubifs"),
    }
    ignore = ["md5sum"]
    needles = aliases[kind]
    matches: list[tuple[str, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = Path(member.name).name
            lowered = name.lower()
            if not any(token in lowered for token in needles) or any(token in lowered for token in ignore):
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            matches.append((name, extracted.read()))
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one {kind} payload in release archive, found {len(matches)}"
        )
    return matches[0]


async def openipc_load_release_assets(
    tftp,
    context: OpenIpcInstallContext,
) -> OpenIpcReleaseAssets:
    manifest = await _load_release_manifest(
        tftp,
        tag=context.tag,
        cache=context.cache,
    )
    uboot_manifest = manifest

    uboot_asset = _find_release_asset_optional(
        manifest,
        soc=context.soc,
        fw=context.fw,
        partition="uboot",
    )
    if uboot_asset is None and context.tag != "latest":
        latest_manifest = await _load_release_manifest(
            tftp,
            tag="latest",
            cache=context.cache,
        )
        uboot_manifest = latest_manifest
        uboot_asset = openipc_find_release_asset(
            latest_manifest,
            soc=context.soc,
            fw=context.fw,
            partition="uboot",
        )
    elif uboot_asset is None:
        uboot_asset = openipc_find_release_asset(
            manifest,
            soc=context.soc,
            fw=context.fw,
            partition="uboot",
        )
    uboot_payload = await uboot_manifest.download_asset(
        uboot_asset,
        destination=_asset_destination(uboot_manifest, uboot_asset, context.soc),
        cache=context.cache,
    )
    # Release assets are standalone U-Boot images, sometimes with the actual
    # U-Boot payload embedded in an XZ member.  Do not infer a flash layout
    # here; extract the compiled-in default environment from the image.
    release_env = extract_default_env_from_uboot(uboot_payload)
    kernel_asset = _find_release_asset_optional(
        manifest,
        soc=context.soc,
        fw=context.fw,
        partition="kernel",
    )
    rootfs_asset = _find_release_asset_optional(
        manifest,
        soc=context.soc,
        fw=context.fw,
        partition="rootfs",
    )
    if kernel_asset is not None and rootfs_asset is not None:
        kernel_payload = await manifest.download_asset(
            kernel_asset,
            destination=_asset_destination(manifest, kernel_asset, context.soc),
            cache=context.cache,
        )
        rootfs_payload = await manifest.download_asset(
            rootfs_asset,
            destination=_asset_destination(manifest, rootfs_asset, context.soc),
            cache=context.cache,
        )
    else:
        bundle_asset = openipc_find_release_asset(
            manifest,
            soc=context.soc,
            fw=context.fw,
            partition="firmware_bundle",
        )
        bundle_payload = await manifest.download_asset(
            bundle_asset,
            destination=_asset_destination(manifest, bundle_asset, context.soc),
            cache=context.cache,
        )
        kernel_name, kernel_payload = _extract_tar_member(bundle_payload, kind="kernel")
        rootfs_name, rootfs_payload = _extract_tar_member(bundle_payload, kind="rootfs")
        kernel_asset = GithubAsset(
            {
                "name": kernel_name,
                "browser_download_url": str(bundle_asset.get("browser_download_url", "")),
            }
        )
        rootfs_asset = GithubAsset(
            {
                "name": rootfs_name,
                "browser_download_url": str(bundle_asset.get("browser_download_url", "")),
            }
        )
    partition_table, mtdparts_spec = _openipc_partition_layout(
        release_env,
        flash_type="nor",
        flash_size=context.nor_size,
        key=None,
        payload_sizes={
            "uboot": len(uboot_payload),
            "kernel": len(kernel_payload),
            "rootfs": len(rootfs_payload),
        },
    )
    return OpenIpcReleaseAssets(
        manifest=manifest,
        release_env=release_env,
        partition_table=partition_table,
        uboot_asset=uboot_asset,
        uboot_payload=uboot_payload,
        kernel_asset=kernel_asset,
        kernel_payload=kernel_payload,
        rootfs_asset=rootfs_asset,
        rootfs_payload=rootfs_payload,
        mtdparts_spec=mtdparts_spec,
    )


def _require_partition(table: PartitionTable, *names: str) -> PartitionEntry:
    for name in names:
        entry = table.get(name)
        if entry is not None:
            offset, size = entry.range(total_size=table.total_size)
            return PartitionEntry(name=entry.name, offset=offset, size=size)
    raise ValueError(f"required partition not found: {', '.join(names)}")


def _source_name(asset: GithubAsset) -> str:
    name = str(asset.get("name", "")).strip()
    if name:
        return name
    url = str(asset.get("browser_download_url", "")).strip()
    if url:
        return _parse_url_filename(url)
    return ""


def openipc_build_partition_payloads(
    tftp,
    context: OpenIpcInstallContext,
    release: OpenIpcReleaseAssets,
) -> tuple[PartitionPayload, ...]:
    uboot_entry = _require_partition(release.partition_table, "uboot", "boot")
    env_entry = _require_partition(release.partition_table, "env")
    kernel_entry = _require_partition(release.partition_table, "kernel")
    rootfs_entry = _require_partition(release.partition_table, "rootfs")

    # TODO: Move to openipc_patch_env
    patched_env = dict(release.release_env)
    if release.mtdparts_spec is None:
        raise ValueError("release bootargs has no mtdparts specification to update")
    context.env['mtdparts_spec'] = release.mtdparts_spec
    msgs = openipc_patch_env(tftp, context.ident, context.env, patched_env)
    env_payload = ubootenv_build(
        patched_env,
        size=env_entry.size,
        little_endian=tftp.is_le,
    )
    tftp.exec_queue(msgs)
    return (
        PartitionPayload(
            name="uboot",
            offset=uboot_entry.offset,
            size=uboot_entry.size,
            payload=release.uboot_payload,
            source=_source_name(release.uboot_asset),
        ),
        PartitionPayload(
            name="env",
            offset=env_entry.offset,
            size=env_entry.size,
            payload=env_payload,
            source=f"{context.ident}-env.bin",
        ),
        PartitionPayload(
            name="kernel",
            offset=kernel_entry.offset,
            size=kernel_entry.size,
            payload=release.kernel_payload,
            source=_source_name(release.kernel_asset),
        ),
        PartitionPayload(
            name="rootfs",
            offset=rootfs_entry.offset,
            size=rootfs_entry.size,
            payload=release.rootfs_payload,
            source=_source_name(release.rootfs_asset),
        ),
    )


def openipc_format_update_summary(plan) -> list[str]:
    return [
        uboot_msg(
            f"{update.name:<8} 0x{update.offset:08x} size=0x{update.size:08x} "
            f"src={_trunc(update.source, 32):<32} flash=0x{update.flash_crc32:08x} "
            f"payload=0x{update.payload_crc32:08x} {'update' if update.needs_update else 'skip'}"
        )
        for update in plan.updates
    ]


def _stage_partition_filename(ident: str, update: PartitionUpdate) -> str:
    return f"install/{Path(update.source).name}"


async def openipc_flash_partition(tftp, ident: str, update: PartitionUpdate) -> None:
    filename = _stage_partition_filename(ident, update)
    tftp.write_file(filename, update.payload)
    requires = []
    tftp.exec_queue(
        [
            uboot_msg(f"Uploading {Path(filename).name}... ", nl=False, bold=True),
            uboot_fetch_static(tftp, filename, offset=FLASH_STAGE_RAM_OFFSET, requires=requires),
            uboot_msg("OK"),
            uboot_msg(f"Erasing {update.name}... ", nl=False, bold=True),
            uboot_nor_erase(offset=update.offset, size=update.size, requires=requires),
            uboot_msg("OK"),
            uboot_msg(f"Writing {update.name}... ", nl=False, bold=True),
            uboot_nor_write(
                tftp,
                nor_offset=update.offset,
                ram_offset=FLASH_STAGE_RAM_OFFSET,
                size=len(update.payload),
                requires=requires,
            ),
            uboot_msg("OK"),
        ],
        requires=requires
    )


async def openipc_erase_overlay(tftp, partition: PartitionEntry) -> None:
    requires = ["sf probe", "sf erase"]
    tftp.exec_queue(
        [
            uboot_msg(
                f"Erasing overlay ({partition.name})... ",
                nl=False,
                bold=True,
            ),
            uboot_nor_erase(
                offset=partition.offset,
                size=partition.size,
            ),
            uboot_msg("OK"),
        ],
        requires=requires,
    )


def _openipc_overlay_partition(
    partition_table: PartitionTable,
    tftp_env: dict[str, str],
) -> PartitionEntry | None:
    if tftp_env.get("erase_overlay") != "1":
        return None
    return _require_partition(partition_table, "rootfs_data", "overlay")


async def openipc_install(tftp, ident: str, cmd: str, tftp_env: dict[str, str]):
    '''
    function: openipc_install - Fully automated openipc install to NOR flash.
    '''
    try:
        requires = []
        context = await openipc_collect_install_context(tftp, ident, cmd, tftp_env)
        release = await openipc_load_release_assets(tftp, context)
        payloads = openipc_build_partition_payloads(tftp, context, release)
        overlay = _openipc_overlay_partition(release.partition_table, tftp_env)
        tftp.exec_queue([
            uboot_msg("Copying NOR flash to RAM... ", bold=True, nl=False),
            uboot_nor_read(
                tftp,
                nor_offset=0,
                ram_offset=FLASH_SNAPSHOT_RAM_OFFSET,
                size=context.nor_size,
                requires=requires,
            ),
            uboot_msg("OK"),
        ], requires=requires)
        plan = await build_partition_update_plan(
            tftp,
            payloads,
            snapshot_base_addr=tftp.rambase_addr + FLASH_SNAPSHOT_RAM_OFFSET,
            key_prefix="openipc_",
        )
        tftp.exec_queue([
                uboot_msg("Partition update plan:", bold=True),
                *openipc_format_update_summary(plan),
        ])
        pending = plan.pending()
        if not pending and overlay is None:
            await tftp.exec(
                [uboot_msg("All target partitions already match release assets.")],
                final=True,
            )
            return
        else:
            await builtin_flash_backup(tftp, ident, {'file': f'openipc_backup_{ident}_{datetime.now():%Y%m%d-%H%M%S}.bin'})
        for update in pending:
            await openipc_flash_partition(tftp, ident, update)
        if overlay is not None:
            await openipc_erase_overlay(tftp, overlay)
        summary = [
            uboot_msg(),
            uboot_msg(f"Install finished for {ident}", bold=True),
        ]
        if pending:
            summary.append(
                uboot_msg(
                    f"Updated partitions: {', '.join(update.name for update in pending)}"
                )
            )
        if overlay is not None:
            summary.append(uboot_msg(f"Erased overlay partition: {overlay.name}"))
        summary.extend([
            uboot_msg(),
            uboot_msg("Type: run persist - to check for updates on reboot"),
            uboot_msg(),
            uboot_msg("Consider supporting OpenIPC: https://opencollective.com/openipc", color='yellow'),
            uboot_msg(),
        ])
        tftp.exec_queue(summary)
        await uboot_exec_delay(
            tftp,
            "Rebooting in 10 seconds",
            10,
            [uboot_msg("Rebooting...", color="white"), "reset"],
            final=True,
        )
    except ValueError as error:
        messages = [uboot_err(line) for line in str(error).splitlines() if line.strip()]
        await tftp.exec(messages or [uboot_err(str(error))], final=True)

async def default(tftp, ident: str, cmd: str, tftp_env: dict[str, str]):
    '''
    function: default - Called when config.toml doesn't have matching id=
    declaration.
    '''

    match cmd:
        case 'openipc_install':
            tftp_env.setdefault('erase_overlay', '1')
            await openipc_install (tftp, ident, cmd, tftp_env)

        case 'openipc_update':
            await openipc_install (tftp, ident, cmd, tftp_env)

        case 'manifest':
            soc = tftp_env.get ('soc', 'gk7205v300')
            tag = tftp_env.get('tag', 'latest')
            path = _openipc_release_path(tag)
            manifest = GithubJsonManifest(tftp, path=path)
            await manifest.load ()
            matches = manifest.find (match=[soc])
            for asset in matches:
                await manifest.download_asset(
                    asset,
                    destination=f"{path}/{soc}/{asset['name']}",
                )
            await tftp.exec ([uboot_msg ()], final=True)

        case 'onboot':
            # Check for updates from selected tag.
            # This will automatically boot on return
            await tftp.exec([
                uboot_msg("Checking for updates..."),
                '; '.join([
                    'if test -n "${openipc_update}"',
                    'then run openipc_update',
                    'else echo "Must run openipc_install first!"',
                    'fi',
                ])
            ], requires=['test'], final=True)
            
        # Unrecognized cmd
        case _:
            await tftp.exec ([
                uboot_err(f"openipc: cmd={cmd} is not recognized."),
            ], final=True)
