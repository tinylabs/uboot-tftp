#!/usr/bin/env python3
"""
Example handler module for uboot-tftp.
Implements installing openipc on ip cameras
"""

from __future__ import annotations

import struct
import json
import re
import random
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from typing import Any, TypedDict

from uboot_tftp.ubootscript import *
from uboot_tftp.ubootops import *
from uboot_tftp.ubootterm import *
from uboot_tftp.ubootenv import *


class GithubAsset(TypedDict, total=False):
    name: str
    browser_download_url: str
    content_type: str
    size: int
    updated_at: str


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
        manifest = self.manifest
        assets = manifest.get("assets", [])
        if not isinstance(assets, list):
            raise TypeError("manifest assets field must be a list")
        return [asset for asset in assets if isinstance(asset, dict)]

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
        asset: GithubAsset,
        *,
        destination: str | None = None,
    ) -> bytes:
        url = str(asset.get("browser_download_url", "")).strip()
        if not url:
            raise ValueError("asset must include browser_download_url")

        name = str(asset.get("name", "")).strip()
        if not name:
            raise ValueError("asset must include name")

        filepath = destination or self._asset_destination(name)
        await uboot_download_url(
            self.tftp,
            url=url,
            filepath=filepath,
            page_url=self.url,
        )
        return self.tftp.read_file(filepath)

    def _asset_destination(self, name: str) -> str:
        return f"{self.path}/{name}"

    async def load(self) -> dict[str, Any]:
        if self._manifest is not None:
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

    
async def openipc_download_binary(tftp, vendor: str, soc: str, size_mb: int, fw: str) -> bytes:
    filename = f"install/openipc-{soc}-{fw}-{size_mb}mb.bin"
    page_url = f"https://openipc.org/cameras/vendors/{quote(vendor)}/socs/{quote(soc)}"
    dl_url = (
        f"https://openipc.org/cameras/vendors/{quote(vendor)}/"
        f"socs/{quote(soc)}/download_full_image"
        f"?flash_size={quote(str(size_mb))}&flash_type=nor&fw_release={quote(fw)}"
    )
    return await uboot_download_url (tftp, filepath=filename, url=dl_url, page_url=page_url, cache=True)


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
    await tftp.exec([
        uboot_msg (f'  Saved backup as {filename}')
    ], final=final)

async def openipc_nor_restore (tftp, filename: str, sz: int):
    script = [
        uboot_msg (f"Uploading {Path(filename).name}... ", nl=False, bold=True),
        uboot_fetch_static (tftp, filename, offset=1024),
        uboot_msg ("OK"),
        uboot_msg ("Erasing flash... ", nl=False, bold=True),
        uboot_nor_erase (offset=0, size=sz),
        uboot_msg ("OK"),
        uboot_msg ("Writing flash... ", nl=False, bold=True),
        uboot_nor_write (tftp, nor_offset=0, ram_offset=1024, size=sz),
        uboot_msg ("OK"),
    ]
    await tftp.exec (script)

def build_runcmd(cmd: str, args: str=''):
    args = f"args={args}" if args else ""
    return '; '.join([
        f"cmd={cmd}",
        f"{args}",
        "run bootstrap"
    ])

def gen_mac (mac: str) -> str:
    if mac in ('00:00:23:34:45:66', '00:00:00:00:00:00'):
        mac_bytes = [0x02] + [random.randint(0x00, 0xFF) for _ in range(5)]
        mac = ":".join(f"{b:02x}" for b in mac_bytes)
    return mac

def trunc(s: str, max_len: int, suffix: str = "...") -> str:
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
        msgs += [uboot_msg(f'+  {k:<10} = {trunc(v, 20)}')]
    for k, v in {key: new_env[key] for key in keys if key in old_env}.items():
        msgs += [uboot_msg(f'>  {k:<10} = {trunc(v, 20)}')]
    return msgs

def check_install_args (tftp, ident: str, cmd: str,
                        env: dict[str, str]) -> list:
    script = []
    if 'vendor' not in env:
        script.append (uboot_err ("Must pass vendor=name"))
    if 'soc' not in env:
        script.append (uboot_err ("Must pass soc=name"))
    if env['fw'] not in ('lite', 'ultimate'):
        script.append (uboot_err (f"Invalid: fw={fw} - Only fw=lite|ultimate supported"))
    if script:
        script.append (uboot_err (f"ie: {tftp.cmdtftp} {tftp.rambase} " +
                                  "{tftp.server_ip}:id={ident}/{cmd}/vendor=goke/soc=gk7205v300/fw=lite; " +
                                  "source {tftp.rambase}"))
    return script

async def openipc_install(tftp, ident: str, cmd: str, tftp_env: dict[str, str]):
    '''
    function: openipc_install - Fully automated openipc install to NOR flash.
    '''

    # Fetch current environment
    cenv = await tftp.fetch_env(
        upload_script=[
            uboot_msg ("Fetching current uboot environment... ", nl=False, bold=True),
        ]
    )
    await tftp.exec([uboot_msg ('OK')])

    # Merge keys from tftp environment (override) if present
    keys = ['nor_size', 'fw', 'vendor', 'soc']
    cenv.update({k: tftp_env[k] for k in keys if k in tftp_env})

    # Set defaults if not present
    cenv.setdefault ('fw', 'lite')
    cenv.setdefault ('ip', 'dhcp')
    cenv.setdefault ('nor_size', None)
    
    # Probe NOR flash
    nor_size = await uboot_nor_probe(
        tftp,
        max_size=tftp_env.get('nor_size', None),
        pre_cmds=[uboot_msg("Probing NOR flash... ", nl=False, bold=True)],
        post_cmds=[uboot_msg('${size}')],
    )
    nor_size_mb = int(nor_size / 2**20)
    
    # Check if we have everything we need in env
    msgs = check_install_args(tftp, ident, cmd, cenv)

    # Validate NOR requirements
    if nor_size == 0:
        msgs += [uboot_err("NOR flash not detected! Aborting...")]
    elif nor_size_mb not in [8, 16]:
        msgs += [uboot_err("Only 8M or 16M NOR flash supported.")]
    elif nor_size_mb < 16 and cenv['fw'] == 'ultimate':
        msg += [uboot_err("fw=ultimate requires 16M flash")]
    if msgs:
        await tftp.exec(msgs, final=True)
        return

    # Collect environment variables
    fw = cenv['fw']
    vendor = cenv["vendor"]
    soc = cenv["soc"]
    filename = f"install/openipc-{soc}-{fw}-{nor_size_mb}mb.bin"
    backup_filename = f'install-backup-{ident}-{soc}-{nor_size_mb}mb-{datetime.now():%Y%m%d-%H%M%S}.bin'

    # Backup NOR memory
    await tftp.exec([uboot_msg('Backing up NOR flash.', bold=True)])
    await openipc_nor_backup(tftp, nor_size, backup_filename)

    # Download official binary
    binary = await openipc_download_binary(tftp, vendor=vendor, soc=soc, fw=fw, size_mb=nor_size_mb)
    if not binary:
        return

    # Extract uboot env from new image
    await tftp.exec ([uboot_msg("Extracting uboot env from image... ", nl=False, bold=True)])
    try:
        new_env = ubootenv_extract(binary)
    except ValueError as err:
        await tftp.exec ([
            uboot_err(f"Failed to extract uboot env from {Path(filename).name}", final=True),
        ])
        return

    # TODO: check if uboot env crc needs to be big endian on MIPS
    # Otherwise patched env won't load on reset
    msgs = [uboot_msg('OK'), uboot_msg('Patched env variables:', bold=True)] + openipc_patch_env(tftp, ident, cenv, new_env)
    await tftp.exec (msgs)
    patched_bin = ubootenv_patch(binary, new_env)
    filename = f'patched/{ident}-{Path(filename).name}'
    tftp.write_file(filename, patched_bin)

    # TODO:
    # - Fetch assets from github latest instead
    # https://api.github.com/repos/OpenIPC/firmware/releases/tags/latest
    # uboot, kernel+rootfs
    # Extract partition table from uboot env variables
    # -> User fetched u-boot as source of truth for partition table
    # mtdparts=sfc:256k(boot),64k(env),3072k(kernel),10240k(rootfs),-(rootfs_data)
    # Take CRC of each partition to check if we need to reflash
    
    # Flash new firmware
    await openipc_nor_restore (tftp, filename, len (patched_bin))

    # Get new IP to display if mac address changed
    if new_env['ip'] != 'static' and (new_env['ethaddr'] != cenv['ethaddr']):
        await tftp.exec ([
            uboot_msg(f"Getting new IP with ethaddr={new_env['ethaddr']}..."),
            f'setenv ethaddr {new_env["ethaddr"]}',
            'setenv autoload no',
            'dhcp',
            uboot_msg("Success: ip=${ipaddr} mask=${netmask} gateway=${gatewayip}")
        ], keys=['ipaddr', 'netmask', 'gatewayip'])
    keys = ['ipaddr', 'netmask', 'gatewayip']
    cenv.update({k: tftp_env[k] for k in keys if k in tftp_env})

    # Print complete message
    await tftp.exec([
        uboot_msg(),
        uboot_msg(f"Install finished for {ident}", bold=True),
        uboot_msg(f"------------------------------"),
        uboot_msg(f"Flash backup: {tftp.root}/{backup_filename}"),
        uboot_msg(f"Web UI: http://{cenv['ipaddr']}/"),
        uboot_msg(f"SSH: ssh root@{cenv['ipaddr']} (password: 12345)"),
        uboot_msg("Support OpenIPC: https://opencollective.com/openipc/contribute"),
        uboot_msg(),
    ])
    await uboot_exec_delay (tftp, "Rebooting in 10 seconds", 10,
                            [uboot_msg ("Rebooting...", color='white'), 'reset'],
                            final=True)

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

def batched(items: list[Range], size: int) -> list[list[Range]]:
    return [items[i:i + size] for i in range(0, len(items), size)]

def split_range(addr: int, size: int, parts: int = 2) -> list[Range]:
    if size % parts != 0:
        raise ValueError(f"range size {size:#x} is not divisible by {parts}")

    chunk = size // parts
    return [(addr + i * chunk, chunk) for i in range(parts)]

def coalesce_ranges(ranges: list[Range]) -> list[Range]:
    if not ranges:
        return []

    ranges = sorted(ranges)

    merged: list[Range] = []
    cur_addr, cur_size = ranges[0]
    cur_end = cur_addr + cur_size

    for addr, size in ranges[1:]:
        end = addr + size

        if addr <= cur_end:
            # Adjacent or overlapping.
            cur_end = max(cur_end, end)
        else:
            merged.append((cur_addr, cur_end - cur_addr))
            cur_addr = addr
            cur_end = end

    merged.append((cur_addr, cur_end - cur_addr))
    return merged

async def find_active(
    tftp,
    tftp_env,
    ranges: list[Range],
    *,
    min_chunk: int = 0x800, # 2kB chunks
    split_parts: int = 2,
    max_ranges: int = 6,
) -> list[Range]:
    cur = ranges

    while cur:
        changed: list[Range] = []

        for batch in batched(cur, max_ranges):
            res1 = await uboot_crc32(tftp, batch)
            res2 = await uboot_crc32(tftp, batch)

            changed.extend(
                r
                for r, x, y in zip(batch, res1, res2)
                if x != y
            )

        if not changed:
            return []

        # Assuming all ranges at this iteration have the same size.
        chunk = changed[0][1]

        if chunk <= min_chunk:
            return coalesce_ranges(changed)

        cur = [
            subrange
            for addr, size in changed
            for subrange in split_range(addr, size, split_parts)
        ]

    return []


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
            filename = env.get ('filename', '')
            await openipc_nor_backup(tftp, sz, filename, final=True)            

        case 'boot':
            await uboot_boot (tftp)

        case 'manifest':
            soc = tftp_env.get ('soc', 'gk7205v300')
            path='OpenIPC/firmware/releases/tags/latest'
            manifest = GithubJsonManifest(tftp, path=path)
            await manifest.load ()
            matches = manifest.find (match=[soc])
            for asset in matches:
                await manifest.download_asset(
                    asset,
                    destination=f"{path}/{soc}/{asset['name']}",
                )
            await tftp.exec ([uboot_msg ()], final=True)

        case 'active':
            ranges = [
                # 16MB ranges - 96MB total
                (0x42000000, 0x1000000), # Dynamic script (changes crc)
                (0x43000000, 0x1000000), # Stable
                (0x44000000, 0x1000000), # Stable
                (0x45000000, 0x1000000), # Stable
                (0x46000000, 0x1000000), # Stable
                (0x47000000, 0x1000000), # TLBs, stack, etc (changes crc)
            ]
            res = await find_active(tftp, tftp_env, ranges)
            cmds = [uboot_msg(f'{hex(addr)}:{hex(length)}', color='yellow')
                    for _, (addr, length) in enumerate(res)]
            await tftp.exec(cmds, final=True)
            
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
            res =  [hex(x) for x in res]
            cmds = [uboot_msg(f'{hex(addr)}:{hex(addr+length-1)} => {res[_]}')
                    for _, (addr, length) in enumerate(ranges)]
            await tftp.exec(cmds, final=True)

        # Unrecognized cmd
        case _:
            await uboot_nomatch(tftp, ident, cmd,
                                cmd_list=['install', 'probe', 'backup', 'boot'])
            await uboot_boot (tftp, delay=10)
            
            
