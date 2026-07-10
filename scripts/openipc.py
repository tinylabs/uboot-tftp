#!/usr/bin/env python3
"""
Example handler module for uboot-tftp.
Implements installing openipc on ip cameras
"""

from __future__ import annotations

import io
import random
import tarfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from uboot_tftp.flashplan import PartitionPayload, PartitionUpdate, build_partition_update_plan
from uboot_tftp.github_assets import GithubAsset, GithubJsonManifest
from uboot_tftp.partitions import PartitionEntry
from uboot_tftp.ubootscript import *
from uboot_tftp.ubootops import *
from uboot_tftp.ubootterm import *
from uboot_tftp.ubootenv import *

OPENIPC_RELEASE_PATH_PREFIX = "OpenIPC/firmware/releases/tags"
FLASH_SNAPSHOT_RAM_OFFSET = 16 * 2**20
FLASH_STAGE_RAM_OFFSET = 1 * 2**20


def openipc_partition_table(
    env: dict[str, str],
    *,
    flash_size: int | None = None,
    flash_type: str | None = None,
    key: str | None = None,
) -> PartitionTable:
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
            continue
        return parse_mtdparts_spec(spec, total_size=flash_size)
    raise ValueError("unable to find an OpenIPC mtdparts specification in environment")


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
    keys.extend(
        sorted(
            name
            for name in env
            if "mtdparts" in name and name not in keys
        )
    )
    return keys


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

async def openipc_nor_backup (tftp, sz: int, filename: str='', final=False) -> bytes:
    if not filename:
        filename = f"snapshot-{datetime.now():%Y%m%d-%H%M%S}.bin"
    binary = await uboot_nor_download(
        tftp,
        sz,
        pre_cmds=[uboot_msg("Copying NOR to RAM... ", bold=True, nl=False)],
        post_cmds=[
            uboot_msg("OK"),
            uboot_msg("Downloading backup via TFTP...", bold=True),
        ],
    )
    filename = f'backup/{filename}'
    tftp.write_file (filename, binary)
    msg = uboot_msg (f'  Saved backup as {filename}')
    await tftp.exec([msg], final=True) if final else tftp.exec_queue([msg])

async def openipc_nor_restore (tftp, filename: str, sz: int, final=False):
    requires = []
    script = [
        uboot_msg (f"Uploading {Path(filename).name}... ", nl=False, bold=True),
        uboot_fetch_static (tftp, filename, offset=1024, requires=requires),
        uboot_msg ("OK"),
        uboot_msg ("Erasing flash... ", nl=False, bold=True),
        uboot_nor_erase (offset=0, size=sz, requires=requires),
        uboot_msg ("OK"),
        uboot_msg ("Writing flash... ", nl=False, bold=True),
        uboot_nor_write (tftp, nor_offset=0, ram_offset=1024, size=sz, requires=requires),
        uboot_msg ("OK"),
    ]
    await tftp.exec (script, requires=requires, final=True) if final else tftp.exec_queue(script, requires=requires)

def build_runcmd(cmd: str, args: str=''):
    parts = [f"cmd={cmd}"]
    if args:
        parts.append(f"args={args}")
    parts.append("run bootstrap")
    return "; ".join(parts)

def gen_mac (mac: str) -> str:
    if mac in ('00:00:23:34:45:66', '00:00:00:00:00:00'):
        mac_bytes = [0x02] + [random.randint(0x00, 0xFF) for _ in range(5)]
        mac = ":".join(f"{b:02x}" for b in mac_bytes)
    return mac

def _trunc(s: str, max_len: int, suffix: str = "...") -> str:
    if len(s) <= max_len:
        return s
    elif max_len <= len(suffix):
        return suffix[:max_len]
    return s[:max_len - len(suffix)] + suffix

def openipc_patch_env(tftp, ident: str, old_env: dict[str,str], new_env: dict[str,str]):
    merge = {
        'ethaddr'    : gen_mac (old_env['ethaddr']),
        'bootp_vci'  : f'uboot.{ident}',
        'hostname'   : ident,
        'install'    : build_runcmd ('install'),
        'backup'     : build_runcmd ('backup'),
        'probe_nor'  : build_runcmd ('probe'),
        'bootstrap'  : '; '.join ([
            'run netinit',
            f'if tftpboot {tftp.rambase} '+'${serverip}:id=${hostname}/${cmd}/${args}',
            f'then source {tftp.rambase}',
            'else echo "TFTP request failed: is TFTP server running?"',
            'fi'
        ]),
        'netinit'    : '; '.join ([
            'if test "${ip}" = "static" || test -n "$netdone" && test "$netdone" -eq 1',
            'then echo "Networking OK"',
            'else setenv autoload no',
            'dhcp',
            'netdone=1',
            'fi'
        ]),
    }
    # Add new entries + merge old > new
    new_env.update({k: merge[k] for k in merge.keys()})
    keys = ['ipaddr', 'netmask', 'gatewayip', 'dnsip', 'serverip', 'fw', 'ip']
    new_env.update({k: old_env[k] for k in keys if k in old_env})

    msgs = []
    for k, v in merge.items():
        msgs += [uboot_msg(f'+  {k:<10} = {_trunc(v, 20)}')]
    for k, v in {key: new_env[key] for key in keys if key in old_env}.items():
        msgs += [uboot_msg(f'>  {k:<10} = {_trunc(v, 20)}')]
    return msgs

def openipc_verify_install_args (tftp, ident: str, cmd: str,
                                 env: dict[str, str]) -> list:
    script = []
    if 'soc' not in env:
        script.append (uboot_err ("Must pass soc=name"))
    if env['fw'] not in ('lite', 'ultimate'):
        script.append (uboot_err (f"Invalid: fw={env['fw']} - Only fw=lite|ultimate supported"))
    if script:
        script.append (uboot_err (f"ie: {tftp.cmdtftp} {tftp.rambase} " +
                                  "{tftp.server_ip}:id={ident}/{cmd}/soc=gk7205v300/fw=lite; " +
                                  "source {tftp.rambase}"))
    return script


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

    keys = ["nor_size", "fw", "soc", "cache", "tag"]
    cenv.update({k: tftp_env[k] for k in keys if k in tftp_env})
    cenv.setdefault("fw", "lite")
    cenv.setdefault("ip", "dhcp")
    cenv.setdefault("nor_size", None)
    cenv.setdefault("cache", "1")
    cenv.setdefault("tag", "latest")

    msgs = openipc_verify_install_args(tftp, ident, cmd, cenv)
    if msgs:
        raise ValueError("\n".join(msgs))

    nor_size = await uboot_nor_probe(
        tftp,
        max_size=tftp_env.get("nor_size", None),
        pre_cmds=[uboot_msg("Probing NOR flash... ", nl=False, bold=True)],
        post_cmds=[uboot_msg("${size}")],
    )
    if nor_size == 0:
        raise ValueError("NOR flash not detected! Aborting...")

    nor_size_mb = int(nor_size / 2**20)
    if nor_size_mb not in (8, 16):
        raise ValueError("Only 8M or 16M NOR flash supported.")
    if nor_size_mb < 16 and cenv["fw"] == "ultimate":
        raise ValueError("fw=ultimate requires 16M flash")
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
    return f"{manifest.path}/{soc}/{_parse_url_filename(url)}"


def _asset_match_groups(soc: str, fw: str, partition: str) -> list[list[str]]:
    if partition == "uboot":
        return [[soc, "u-boot"], [soc, fw, "u-boot"]]
    if partition == "firmware_bundle":
        return [[soc, "nor", fw, ".tgz"], [soc, "nor", fw, ".tar.gz"]]
    return [[soc, fw, partition], [soc, partition]]


def openipc_find_release_asset(
    manifest: GithubJsonManifest,
    *,
    soc: str,
    fw: str,
    partition: str,
) -> GithubAsset:
    for needles in _asset_match_groups(soc, fw, partition):
        matches = manifest.find(match=needles)
        if len(matches) == 1:
            return matches[0]
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
    manifest = GithubJsonManifest(
        tftp,
        path=_openipc_release_path(context.tag),
        cache=context.cache,
    )
    await manifest.load()

    uboot_asset = openipc_find_release_asset(
        manifest,
        soc=context.soc,
        fw=context.fw,
        partition="uboot",
    )
    uboot_payload = await manifest.download_asset(
        uboot_asset,
        destination=_asset_destination(manifest, uboot_asset, context.soc),
        cache=context.cache,
    )
    release_env = ubootenv_extract(uboot_payload)
    partition_table = openipc_partition_table(
        release_env,
        flash_type="nor",
        flash_size=context.nor_size,
    )

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

    patched_env = dict(release.release_env)
    openipc_patch_env(tftp, context.ident, context.env, patched_env)
    env_payload = ubootenv_build(
        patched_env,
        size=env_entry.size,
        little_endian=tftp.is_le,
    )

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

async def openipc_install(tftp, ident: str, cmd: str, tftp_env: dict[str, str]):
    '''
    function: openipc_install - Fully automated openipc install to NOR flash.
    '''
    try:
        requires = []
        context = await openipc_collect_install_context(tftp, ident, cmd, tftp_env)
        release = await openipc_load_release_assets(tftp, context)
        payloads = openipc_build_partition_payloads(tftp, context, release)
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
        if not pending:
            await tftp.exec(
                [uboot_msg("All target partitions already match release assets.")],
                final=True,
            )
            return
        for update in pending:
            await openipc_flash_partition(tftp, ident, update)
        tftp.exec_queue(
            [
                uboot_msg(),
                uboot_msg(f"Install finished for {ident}", bold=True),
                uboot_msg(f"Updated partitions: {', '.join(update.name for update in pending)}"),
                uboot_msg(),
            ]
        )
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

async def uboot_nomatch(tftp, ident: str, cmd: str, cmd_list: list=None, final: bool=False) -> None:
    ''' Throw error for no matching entry '''

    cmds = str (cmd_list) if cmd_list else ''
    await tftp.exec ([
        uboot_err(f"uboot-tftp: No matching entry for: id={ident}"),
        uboot_err(f"uboot-tftp: cmd={cmd} is not recognized."),
        uboot_msg(f"uboot-tftp: valid cmds = {cmd_list}"),        
        uboot_msg(f"Add snippet to uboot-tftp config.toml:", color="yellow"),
        uboot_msg(f"[{ident}]", color="yellow"),
        uboot_msg(f"function=<python function name>", color="yellow"),
        uboot_msg()
    ], final=final)

Range = tuple[int, int]


async def default(tftp, ident: str, cmd: str, tftp_env: dict[str, str]):
    '''
    function: default - Called when config.toml doesn't have matching id=
    declaration.
    '''

    match cmd:
        case 'install':
            await openipc_install (tftp, ident, cmd, tftp_env)

        case 'probe':
            sz = await uboot_nor_probe(
                tftp,
                max_size=tftp_env.get('nor_size', None),
                pre_cmds=[uboot_msg("Probing NOR flash... ", nl=False, bold=True)],
                post_cmds=[uboot_msg('${size}')],
                final=True,
            )

        case 'backup':
            sz = await uboot_nor_probe(
                tftp,
                max_size=tftp_env.get('nor_size', None),
                pre_cmds=[uboot_msg("Probing NOR flash... ", nl=False, bold=True)],
                post_cmds=[uboot_msg('${size}')],
            )
            filename = tftp_env.get ('filename', '')
            await openipc_nor_backup(tftp, sz, filename, final=True)            

        case 'boot':
            await uboot_boot (tftp)

        case 'manifest':
            soc = tftp_env.get ('soc', 'gk7205v300')
            tag = tftp_env.get('tag', 'latest')
            path = _openipc_release_path(tag)
            manifest = GithubJsonManifest(tftp, path=path)
            await manifest.load ()
            matches = manifest.find (match=[soc, 'u-boot'])
            for asset in matches:
                await manifest.download_asset(
                    asset,
                    destination=f"{path}/{soc}/{asset['name']}",
                )
            await tftp.exec ([uboot_msg ()], final=True)

        case 'crc32':
            ranges = [
                # 16MB ranges - 96MB total
                (0x42000000, 0x1000000), # Dynamic script (changes crc)
                (0x43000000, 0x1000000), # Stable
                (0x44000000, 0x1000000), # Stable
                (0x45000000, 0x1000000), # Stable
                (0x46000000, 0x1000000), # Stable
                (0x47000000, 0x1000000), # TLBs, stack, etc (changes crc)
            ]

            res = await uboot_crc32(tftp, ranges)
            cmds = [
                uboot_msg(f'0x{addr:08x}:0x{addr + length - 1:08x} => 0x{res[_]:08x}')
                for _, (addr, length) in enumerate(ranges)
            ]
            await tftp.exec(cmds, final=True)

        case 'cmd_check':
            cmds = ['cmdtftpput']
            # This will force a check
            ok = await tftp.exec([
                uboot_msg(f'{cmds} present'),
            ], requires=cmds)
            # supported cmds will be cached here...
            ok = await tftp.exec([
                uboot_msg(f'{cmds} present'),
            ], requires=cmds)
            if not ok:
                msg = uboot_err('failed')
            else:
                msg = uboot_msg('passed')
            await tftp.exec([msg], final=True)
                        
        # Unrecognized cmd
        case _:
            await uboot_nomatch(tftp, ident, cmd,
                                cmd_list=['install', 'probe', 'backup', 'boot', 'manifest', 'crc32'])
            await uboot_boot (tftp, delay=10)
            
            
