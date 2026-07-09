"""Helpers for parsing and extracting U-Boot environments."""

from __future__ import annotations

import bz2
import lzma
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass

from .partitions import extract_mtdparts_spec, parse_mtdparts_spec

_ENV_KEY_RE = re.compile(rb"^[A-Za-z0-9_.-]+$")
_ENV_BLOB_RE = re.compile(
    rb"(?:[A-Za-z0-9_.-]{1,64}=[^\x00\r\n]{0,1024}\x00){5,}"
)
_RAW_ENV_PREFIX_RE = re.compile(rb"^[A-Za-z0-9_.-]+=")

_CORE_ENV_KEYS = (
    "bootcmd",
    "bootargs",
    "mtdparts",
    "baudrate",
    "ipaddr",
    "serverip",
)
_ENV_MARKERS = tuple(
    f"{key}=".encode("ascii")
    for key in (
        "bootcmd",
        "bootargs",
        "mtdparts",
        "baudrate",
        "stdin",
        "stdout",
        "stderr",
    )
)
_UBOOT_MARKERS = (
    b"env_common.c",
    b"nvedit.c",
    b"U-Boot",
)
_COMMON_ENV_SIZES = (0x10000, 0x20000, 0x40000)
_POST_BOOT_MAGICS = (
    b"\x27\x05\x19\x56",  # uImage
    b"\xd0\x0d\xfe\xed",  # FDT
    b"hsqs",  # SquashFS
    b"UBI#",  # UBI
)


@dataclass(frozen=True)
class EnvPartitionInfo:
    offset: int
    size: int


def ubootenv_parse_export(body: bytes, *, encoding: str = "utf-8") -> dict[str, str]:
    """Parse `env export -t` output into a dictionary.

    U-Boot text exports are typically NUL-delimited, but this parser also accepts
    newline-delimited content to make testing and local tooling simpler.
    """

    env: dict[str, str] = {}
    text = body.decode(encoding, errors="replace").replace("\0", "\n")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key:
            env[key] = value
    return env


def ubootenv_parse_part(partition: bytes) -> dict[str, str]:
    """Parse a dedicated U-Boot env partition into a dictionary."""

    if not partition:
        raise ValueError("env partition is empty")
    if _is_erased(partition):
        raise ValueError("env partition is erased")

    for header_size in (4, 5):
        if len(partition) <= header_size:
            continue
        payload = partition[header_size:]
        payload_crc = zlib.crc32(payload) & 0xFFFFFFFF
        expected_crc_be = int.from_bytes(partition[:4], "big")
        expected_crc_le = int.from_bytes(partition[:4], "little")
        if payload_crc not in (expected_crc_be, expected_crc_le):
            continue
        env = _parse_nul_delimited_env(payload)
        if env:
            return env

    raw_env = _parse_raw_env_partition(partition)
    if raw_env is not None:
        return raw_env

    raise ValueError("env partition is not a valid U-Boot environment")


def ubootenv_build(
    env: dict[str, str],
    *,
    size: int | None = None,
    flags: int | None = None,
    pad_byte: int = 0x00,
    encoding: str = "utf-8",
) -> bytes:
    """Build a CRC-prefixed U-Boot env image from key/value pairs."""

    if not 0 <= pad_byte <= 0xFF:
        raise ValueError("pad_byte must be between 0x00 and 0xFF")
    if flags is not None and not 0 <= flags <= 0xFF:
        raise ValueError("flags must be between 0x00 and 0xFF")

    payload = _encode_env_image_payload(env, encoding=encoding)
    header_size = 4 if flags is None else 5
    if size is None:
        data = payload
    else:
        data_size = size - header_size
        if data_size < len(payload):
            raise ValueError("size is too small for the encoded environment")
        data = payload + bytes([pad_byte]) * (data_size - len(payload))

    crc = zlib.crc32(data) & 0xFFFFFFFF
    header = crc.to_bytes(4, "little")
    if flags is not None:
        header += bytes([flags])
    return header + data


def extract_default_env_from_uboot(boot_region: bytes) -> dict[str, str]:
    """Extract the embedded default env from a U-Boot boot partition."""

    best_env: dict[str, str] | None = None
    best_score = -1

    for _, candidate in _iter_uboot_candidates(boot_region):
        candidate_score = _score_candidate_bytes(candidate)
        for match in _ENV_BLOB_RE.finditer(candidate):
            env = _parse_nul_delimited_env(match.group(0))
            if not _is_plausible_embedded_env(env):
                continue
            match_span = match.end() - match.start()
            score = candidate_score + _score_env(env) + min(match_span, 4096) // 64
            if score > best_score:
                best_score = score
                best_env = env

    if best_env is None:
        raise ValueError("unable to locate an embedded default U-Boot environment")
    return best_env


def ubootenv_extract(
    image: bytes,
    *,
    boot_offset: int = 0,
    boot_size: int | None = None,
    env_offset: int | None = None,
    env_size: int | None = None,
) -> dict[str, str]:
    """Extract the effective U-Boot env from a flash image or standalone U-Boot image."""

    boot_size, env_offset, env_size, default_env = _resolve_partition_layout(
        image,
        boot_offset=boot_offset,
        boot_size=boot_size,
        env_offset=env_offset,
        env_size=env_size,
    )

    partition = image[env_offset : env_offset + env_size]
    if len(partition) != env_size:
        if default_env is not None:
            return default_env
        boot_region = image[boot_offset : boot_offset + boot_size]
        if len(boot_region) != boot_size:
            raise ValueError("boot partition extends beyond the flash image")
        try:
            return extract_default_env_from_uboot(boot_region)
        except ValueError as boot_error:
            raise ValueError(
                "env partition extends beyond the flash image; also failed to "
                f"extract embedded default env: {boot_error}"
            ) from boot_error

    try:
        return ubootenv_parse_part(partition)
    except ValueError as partition_error:
        if default_env is None:
            boot_region = image[boot_offset : boot_offset + boot_size]
            if len(boot_region) != boot_size:
                raise ValueError(
                    "boot partition extends beyond the flash image"
                ) from partition_error
            try:
                default_env = extract_default_env_from_uboot(boot_region)
            except ValueError as boot_error:
                message = (
                    f"{partition_error}; also failed to extract embedded "
                    f"default env: {boot_error}"
                )
                raise ValueError(
                    message
                ) from boot_error
        return default_env


def ubootenv_find(
    image: bytes,
    *,
    boot_offset: int = 0,
    boot_size: int | None = None,
    env_offset: int | None = None,
    env_size: int | None = None,
) -> EnvPartitionInfo:
    """Return the env partition offset and size from a full flash image."""

    _, resolved_env_offset, resolved_env_size, _ = _resolve_partition_layout(
        image,
        boot_offset=boot_offset,
        boot_size=boot_size,
        env_offset=env_offset,
        env_size=env_size,
    )
    return EnvPartitionInfo(offset=resolved_env_offset, size=resolved_env_size)


def ubootenv_patch(
    image: bytes,
    env: dict[str, str],
    *,
    boot_offset: int = 0,
    boot_size: int | None = None,
    env_offset: int | None = None,
    env_size: int | None = None,
    flags: int | None = None,
    pad_byte: int = 0xFF,
    encoding: str = "utf-8",
) -> bytes:
    """Return a flash image with the env partition replaced."""

    info = ubootenv_find(
        image,
        boot_offset=boot_offset,
        boot_size=boot_size,
        env_offset=env_offset,
        env_size=env_size,
    )
    env_image = ubootenv_build(
        env,
        size=info.size,
        flags=flags,
        pad_byte=pad_byte,
        encoding=encoding,
    )
    return (
        image[: info.offset]
        + env_image
        + image[info.offset + info.size :]
    )


def _resolve_partition_layout(
    image: bytes,
    *,
    boot_offset: int,
    boot_size: int | None,
    env_offset: int | None,
    env_size: int | None,
) -> tuple[int, int, int, dict[str, str] | None]:
    if not image:
        raise ValueError("flash image is empty")
    if boot_offset < 0:
        raise ValueError("boot_offset must be non-negative")

    default_env: dict[str, str] | None = None

    if boot_size is None and env_offset is not None:
        boot_size = env_offset - boot_offset
    if env_offset is None and boot_size is not None:
        env_offset = boot_offset + boot_size

    if boot_size is None or env_offset is None or env_size is None:
        inferred_boot_size, inferred_env_offset, inferred_env_size, default_env = (
            _infer_partition_layout(image, boot_offset=boot_offset, boot_size=boot_size)
        )
        if boot_size is None:
            boot_size = inferred_boot_size
        if env_offset is None:
            env_offset = inferred_env_offset
        if env_size is None:
            env_size = inferred_env_size

    if boot_size is None or env_offset is None or env_size is None:
        raise ValueError("unable to determine boot and env partition boundaries")
    if boot_size <= 0 or env_size <= 0 or env_offset < boot_offset:
        raise ValueError("invalid boot/env partition boundaries")

    return boot_size, env_offset, env_size, default_env


def _infer_partition_layout(
    image: bytes,
    *,
    boot_offset: int,
    boot_size: int | None,
) -> tuple[int, int, int, dict[str, str]]:
    probe_size = (
        boot_size
        if boot_size is not None
        else _guess_boot_probe_size(image, boot_offset)
    )
    probe_region = image[boot_offset : boot_offset + probe_size]
    if len(probe_region) != probe_size:
        raise ValueError("boot probe region extends beyond the flash image")

    default_env = extract_default_env_from_uboot(probe_region)
    layout = _find_mtdparts_layout(default_env)
    if layout is not None:
        inferred_boot_size, inferred_env_size = layout
        return (
            inferred_boot_size,
            boot_offset + inferred_boot_size,
            inferred_env_size,
            default_env,
        )

    boundary = _find_first_post_boot_magic(image, boot_offset)
    if boundary is None:
        raise ValueError("unable to infer flash layout from post-boot image markers")

    for candidate_env_size in _COMMON_ENV_SIZES:
        candidate_env_offset = boundary - candidate_env_size
        if candidate_env_offset <= boot_offset:
            continue
        candidate_boot_region = image[boot_offset:candidate_env_offset]
        try:
            default_env = extract_default_env_from_uboot(candidate_boot_region)
        except ValueError:
            continue
        return (
            candidate_env_offset - boot_offset,
            candidate_env_offset,
            candidate_env_size,
            default_env,
        )

    raise ValueError(
        "unable to infer boot/env partition boundaries from the flash image"
    )


def _guess_boot_probe_size(image: bytes, boot_offset: int) -> int:
    boundary = _find_first_post_boot_magic(image, boot_offset)
    if boundary is not None and boundary > boot_offset:
        return boundary - boot_offset
    return min(len(image) - boot_offset, 2 * 1024 * 1024)


def _find_first_post_boot_magic(image: bytes, boot_offset: int) -> int | None:
    search_start = boot_offset + 0x10000
    offsets = [
        image.find(magic, search_start)
        for magic in _POST_BOOT_MAGICS
        if image.find(magic, search_start) >= 0
    ]
    if not offsets:
        return None
    return min(offsets)


def _find_mtdparts_layout(env: dict[str, str]) -> tuple[int, int] | None:
    ordered_keys = ["mtdparts"]
    ordered_keys.extend(sorted(key for key in env if key.startswith("mtdpartsnor")))
    ordered_keys.extend(
        sorted(
            key
            for key in env
            if "mtdparts" in key and key not in ordered_keys
        )
    )

    for key in ordered_keys:
        value = env.get(key)
        if value is None:
            continue
        spec = extract_mtdparts_spec(value)
        if spec is None:
            continue
        try:
            table = parse_mtdparts_spec(spec)
        except ValueError:
            continue
        boot = table.get("boot")
        env_part = table.get("env")
        if boot is None or env_part is None or boot.size is None or env_part.size is None:
            continue
        return boot.size, env_part.size
    return None


def _parse_nul_delimited_env(body: bytes) -> dict[str, str]:
    env: dict[str, str] = {}
    for segment in body.rstrip(b"\x00\xff").split(b"\x00"):
        if not segment or b"=" not in segment:
            continue
        key, value = segment.split(b"=", 1)
        if not key or not _ENV_KEY_RE.fullmatch(key):
            continue
        env[key.decode("ascii")] = value.decode("utf-8", errors="replace")
    return env


def _parse_raw_env_partition(partition: bytes) -> dict[str, str] | None:
    payload = partition.lstrip(b"\x00\xff")
    if not payload or _RAW_ENV_PREFIX_RE.match(payload) is None:
        return None

    text = payload.split(b"\xff", 1)[0].rstrip(b"\x00")
    env = ubootenv_parse_export(text)
    if len(env) < 2:
        return None
    return env


def _is_erased(region: bytes) -> bool:
    return all(byte == 0xFF for byte in region) or all(byte == 0x00 for byte in region)


def _encode_env_image_payload(env: dict[str, str], *, encoding: str) -> bytes:
    segments: list[bytes] = []
    for key, value in env.items():
        key_bytes = key.encode("ascii")
        if not key_bytes or _ENV_KEY_RE.fullmatch(key_bytes) is None:
            raise ValueError(f"invalid U-Boot env key: {key!r}")
        value_bytes = value.encode(encoding)
        if b"\x00" in value_bytes:
            raise ValueError(f"invalid U-Boot env value for key {key!r}: contains NUL")
        segments.append(key_bytes + b"=" + value_bytes + b"\x00")
    return b"".join(segments) + b"\x00"


def _iter_uboot_candidates(boot_region: bytes) -> Iterator[tuple[str, bytes]]:
    queue: list[tuple[str, bytes, int]] = [("raw", boot_region, 0)]
    seen: set[tuple[int, int]] = set()

    while queue:
        label, candidate, depth = queue.pop(0)
        signature = (len(candidate), zlib.crc32(candidate[:4096]) & 0xFFFFFFFF)
        if signature in seen:
            continue
        seen.add(signature)
        yield label, candidate

        if depth >= 1:
            continue
        for kind, offset, payload in _scan_compressed_members(candidate):
            queue.append((f"{kind}@0x{offset:x}", payload, depth + 1))


def _scan_compressed_members(data: bytes) -> Iterator[tuple[str, int, bytes]]:
    seen_offsets: set[tuple[str, int]] = set()

    for offset in range(max(len(data) - 6, 0)):
        kinds: list[str] = []
        if data.startswith(b"\x1f\x8b\x08", offset):
            kinds.append("gzip")
        if data.startswith(b"\xfd7zXZ\x00", offset):
            kinds.append("xz")
        if data.startswith(b"BZh", offset):
            kinds.append("bzip2")
        if _looks_like_zlib_header(data, offset):
            kinds.append("zlib")

        for kind in kinds:
            marker = (kind, offset)
            if marker in seen_offsets:
                continue
            seen_offsets.add(marker)
            payload = _decompress_member(kind, data[offset:])
            if payload is None or len(payload) < 128:
                continue
            yield kind, offset, payload


def _looks_like_zlib_header(data: bytes, offset: int) -> bool:
    if offset + 2 > len(data):
        return False
    cmf = data[offset]
    flg = data[offset + 1]
    return cmf == 0x78 and ((cmf << 8) + flg) % 31 == 0


def _decompress_member(kind: str, body: bytes) -> bytes | None:
    try:
        if kind == "gzip":
            decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
            output = decomp.decompress(body)
            return output if decomp.eof else None
        if kind == "xz":
            decomp = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
            output = decomp.decompress(body)
            return output if decomp.eof else None
        if kind == "bzip2":
            decomp = bz2.BZ2Decompressor()
            output = decomp.decompress(body)
            return output if decomp.eof else None
        if kind == "zlib":
            decomp = zlib.decompressobj()
            output = decomp.decompress(body)
            return output if decomp.eof else None
    except (OSError, EOFError, lzma.LZMAError, zlib.error):
        return None
    return None


def _score_candidate_bytes(candidate: bytes) -> int:
    marker_hits = sum(1 for marker in _ENV_MARKERS if marker in candidate)
    uboot_hits = sum(1 for marker in _UBOOT_MARKERS if marker in candidate)
    return marker_hits * 8 + uboot_hits * 4


def _score_env(env: dict[str, str]) -> int:
    core_hits = sum(1 for key in _CORE_ENV_KEYS if key in env)
    console_hits = sum(1 for key in ("stdin", "stdout", "stderr") if key in env)
    return core_hits * 100 + console_hits * 20 + len(env) * 10


def _is_plausible_embedded_env(env: dict[str, str]) -> bool:
    core_hits = sum(1 for key in _CORE_ENV_KEYS if key in env)
    return len(env) >= 5 and core_hits >= 2


# Backward-compatible aliases.
