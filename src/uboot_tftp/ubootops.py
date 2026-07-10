"""High-level async U-Boot session operations."""

from __future__ import annotations

import logging
import struct
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from .ubootscript import (
    uboot_crc32_gen,
    uboot_memset,
    uboot_nor_gen_probe,
    uboot_nor_read,
)
from .ubootterm import uboot_err, uboot_msg, uboot_progress, uboot_status, uboot_status_complete

LOGGER = logging.getLogger(__name__)


def _download_progress_lines(artifact) -> str:
    kib = artifact.bytes_done / 1024
    script = [uboot_status(f"{kib:.1f} kB")]
    if artifact.state == 'done':
        script += [uboot_status_complete()]
    return script

def url_validate(url: str) -> str:
    try:
        _ = urlparse (url)
    except Exception:
        return f"Invalid URL: {url}"

async def uboot_download_url(
        tftp,
        url: str,
        filepath: str | Path,
        page_url: str | None = None,
        headers: dict[str, str] | None = None,
        cache=False,
) -> bytes:
    """ Download a URL and return the payload, print status to console. """
    
    if cache and tftp.file_exists(filepath):
        await tftp.exec([uboot_msg(f"Using cached download: {filepath}", bold=True)])
        return tftp.read_file(filepath)

    if msg := url_validate(url):
        await tftp.exec([uboot_err(msg)])
    if page_url and (msg := url_validate(page_url)):
        await tftp.exec([uboot_err(msg)])
    if msg:
        return b''
    
    artifact_key = url
    tftp.acquire_download(
        artifact_key=artifact_key,
        url=url,
        destination=filepath,
        page_url=page_url,
        headers=headers,
    )
    await tftp.exec([uboot_msg(f"Downloading {filepath}: ", nl=False, bold=True)])
    while True:
        artifact = tftp.get_download(artifact_key)
        await tftp.exec(_download_progress_lines(artifact))
        if artifact.state == "done":
            return tftp.read_file(filepath)
        if artifact.state == "failed":
            await tftp.exec([uboot_err(f"Download failed: {artifact.error}")], final=True)
            return b""

async def uboot_nor_download(
    tftp: Any,
    size: int,
    *,
    pre_cmds: Iterable[str] = (),
    post_cmds: Iterable[str] = (),
) -> bytes:
    """Read a NOR flash range into RAM and upload it back to the TFTP server."""

    requires=[]
    script = [
        *_normalize_cmds(pre_cmds),
        uboot_memset(tftp, offset=0, size=size, value=0xFF, requires=requires),
        uboot_nor_read(tftp, ram_offset=0, nor_offset=0, size=size, requires=requires),
        *_normalize_cmds(post_cmds),
    ]
    return await tftp.exec_recv(script=script, size=size, requires=requires)


async def uboot_nor_probe(
    tftp: Any,
    *,
    max_size: int | str | None = None,
    pre_cmds: Iterable[str] = (),
    post_cmds: Iterable[str] = (),
    final: bool = False,
    status_key: str = "status",
    size_key: str = "size",
) -> int:
    """Probe NOR flash and return the detected size in bytes."""

    requires=['sf probe', 'setenv']
    parsed_max_size = _parse_max_size(max_size)
    await tftp.exec(
        [
            *_normalize_cmds(pre_cmds),
            "sf probe 0",
            f"setenv {status_key} $?",
        ],
        keys=[status_key],
        requires=requires)
    if tftp.env[status_key] == "1":
        return 0
    await tftp.exec(
        [
            *uboot_nor_gen_probe(tftp, 2**20, parsed_max_size, requires=requires),
            *_normalize_cmds(post_cmds),
        ],
        keys=[size_key],
        requires=requires,
        final=final,
    )
    return int(tftp.env[size_key], 0)


async def uboot_exec_delay(
    tftp: Any,
    message: str,
    seconds: int,
    cmds: Iterable[str],
    *,
    final: bool = False,
) -> None:
    """Show an interactive countdown before executing commands."""

    intro = [
        uboot_msg(message, color="white"),
        uboot_msg("Enter Ctrl+C to cancel...", color="white"),
    ]
    width = max(int(seconds), 0)
    for step in range(width):
        if step == 0:
            await tftp.exec([*intro, uboot_progress(step, width, color='white')])
        else:
            await tftp.exec([uboot_progress(step, width, color='white')])
    await tftp.exec([*_normalize_cmds(cmds)], final=final)


async def uboot_boot(tftp: Any, *, delay: int = 0) -> None:
    """Boot the device after an optional interactive delay."""

    await uboot_exec_delay(
        tftp,
        f"Booting in {delay}s",
        delay,
        [
            uboot_msg("uboot-tftp: Executing normal boot..."),
            "boot",
        ],
        final=True,
    )


async def uboot_crc32(
    tftp: Any,
    ranges: Iterable[tuple[int, int]],
    *,
    scratch: int | str | None = None,
    little_endian: bool | None = None,
    pre_cmds: Iterable[str] = (),
    post_cmds: Iterable[str] = (),
    final: bool = False,
    key_prefix: str = "c",
) -> list[int]:
    """Compute CRC32 values for one or more memory ranges."""

    batch = list(ranges)
    if not batch:
        return []
    if len(batch) > 6:
        msg = "uboot_crc32 supports at most 6 ranges per call"
        LOGGER.error(
            "Rejecting CRC32 request with too many ranges: count=%d max=6 ident=%s",
            len(batch),
            getattr(tftp, "ident", None),
        )
        await tftp.exec([uboot_err(msg)], final=True)
        raise ValueError(msg)
    endian = _resolve_little_endian(tftp, little_endian)
    scratch_addr = scratch if scratch is not None else tftp.rambase
    keys = [f"{key_prefix}{index}" for index in range(len(batch))]
    script = [*_normalize_cmds(pre_cmds)]
    requires = []
    for index, (addr, length) in enumerate(batch):
        script.extend(
            uboot_crc32_gen(
                addr,
                length,
                scratch=scratch_addr,
                result=keys[index],
                requires=requires,
            )
        )
    script.extend(_normalize_cmds(post_cmds))
    await tftp.exec(script, keys=keys, requires=requires, final=final)

    values: list[int] = []
    for key in keys:
        raw = bytes.fromhex(tftp.env[key].zfill(8))
        values.append(struct.unpack("<I" if endian else ">I", raw)[0])
    return values


def _normalize_cmds(cmds: Iterable[str]) -> list[str]:
    return [cmd for cmd in cmds if cmd]


def _resolve_little_endian(tftp: Any, little_endian: bool | None) -> bool:
    if little_endian is not None:
        return little_endian
    value = getattr(tftp, "is_le", None)
    if value is None:
        raise ValueError("little_endian must be provided when tftp.is_le is unavailable")
    return bool(value)


def _parse_max_size(max_size: int | str | None) -> int:
    if max_size is None:
        return 128 * 2**20
    if isinstance(max_size, int):
        return max_size
    text = max_size.strip()
    if text.upper().endswith("M"):
        return int(text[:-1], 0) * 2**20
    return int(text, 0)
