"""Helpers for building U-Boot script snippets."""

from __future__ import annotations

import itertools
import re
from typing import Any

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_#.-]*$")
_TMP_COUNTER = itertools.count(0)

from uboot_tftp.ubootterm import RESTORE_CURSOR, CLEAR_REGION

def uboot_memset(
    tftp: Any,
    offset: int | str,
    value: int | str,
    size: int | str,
    *,
    base: str | None = None,
) -> str:
    """Return a U-Boot snippet that fills memory relative to a base address."""

    addr_var = _next_tmp("addr")
    base_expr = _normalize_base(tftp, base)
    return "\n".join(
        (
            f"setexpr {addr_var} {base_expr} + {_format_number(offset)}",
            f"mw.b ${{{addr_var}}} {_format_number(value)} {_format_number(size)}",
            f"setenv {addr_var}",
        )
    )

def uboot_memcpy(
    tftp: Any,
    dst_offset: int | str,
    src_offset: int | str,
    size: int | str,
    *,
    base: str | None = None,
) -> str:
    """Return a U-Boot snippet that copies memory relative to a base address."""

    src_var = _next_tmp("src")
    dst_var = _next_tmp("dst")
    base_expr = _normalize_base(tftp, base)
    return "\n".join(
        (
            f"setexpr {src_var} {base_expr} + {_format_number(src_offset)}",
            f"setexpr {dst_var} {base_expr} + {_format_number(dst_offset)}",
            f"cp.b ${{{src_var}}} ${{{dst_var}}} {_format_number(size)}",
            f"setenv {src_var}",
            f"setenv {dst_var}",
        )
    )


def uboot_nor_erase(offset: int | str, size: int | str) -> str:
    """Return a U-Boot snippet that erases a NOR flash range."""

    return "\n".join(
        (
            "sf probe 0",
            f"sf erase {_format_number(offset)} {_format_number(size)}",
        )
    )


def uboot_nor_read(
    tftp: Any,
    ram_offset: int | str,
    nor_offset: int | str,
    size: int | str,
    *,
    base: str | None = None,
) -> str:
    """Return a U-Boot snippet that reads NOR flash into RAM."""

    addr_var = _next_tmp("addr")
    base_expr = _normalize_base(tftp, base)
    return "\n".join(
        (
            "sf probe 0",
            f"setexpr {addr_var} {base_expr} + {_format_number(ram_offset)}",
            f"sf read ${{{addr_var}}} {_format_number(nor_offset)} {_format_number(size)}",
            f"setenv {addr_var}",
        )
    )


def uboot_nor_write(
    tftp: Any,
    nor_offset: int | str,
    ram_offset: int | str,
    size: int | str,
    *,
    base: str | None = None,
) -> str:
    """Return a U-Boot snippet that writes RAM into NOR flash."""

    addr_var = _next_tmp("addr")
    base_expr = _normalize_base(tftp, base)
    return "\n".join(
        (
            "sf probe 0",
            f"setexpr {addr_var} {base_expr} + {_format_number(ram_offset)}",
            f"sf write ${{{addr_var}}} {_format_number(nor_offset)} {_format_number(size)}",
            f"setenv {addr_var}",
        )
    )


def uboot_fetch_static(
    tftp: Any,
    filename: str,
    *,
    offset: int | str | None = None,
    base: str | None = None,
) -> str:
    """Return a U-Boot snippet that downloads a static file into RAM."""

    addr_var = _next_tmp("addr")
    base_expr = _normalize_base(tftp, base)
    remote_path = str(filename).lstrip("/")
    if offset is None:
        return f'tftpboot {tftp.rambase} "{tftp.server_ip}:{remote_path}"'
    return "\n".join(
        (
            f"setexpr {addr_var} {base_expr} + {_format_number(offset)}",
            f'tftpboot ${{{addr_var}}} "{tftp.server_ip}:{remote_path}"',
            f"setenv {addr_var}",
        )
    )

def uboot_nor_gen_probe(
    tftp: Any,
    sz: int,
    max_sz: int,
    script: list[str] | None = None,
    *,
    known_good: int = 0,
    offset: int = 0,
    base: str | None = None,
) -> list[str]:
    """ Return uboot snippet to probe NOR and return size in 'size'"""

    base_expr = _normalize_base(tftp, base)
    if script is None:
        script = []
    if sz > max_sz:
        script.append(f"size={known_good:#x};\n")
        return script
    script.append(f"echo {RESTORE_CURSOR}{CLEAR_REGION}")
    script.append(f"sf read {base_expr} {offset:#x} {sz:#x};\n")
    script.append("if test $? -eq 1; then\n")
    script.append(f"size={known_good:#x};\n")
    script.append("else\n")
    uboot_nor_gen_probe(tftp, sz * 2, max_sz, script,
                        known_good=sz, offset=offset, base=base)
    script.append("fi;\n")
    return script


def uboot_crc32_gen(
    addr: int | str,
    length: int | str,
    *,
    scratch: int | str,
    result: str,
) -> list[str]:
    """Return a U-Boot snippet that computes CRC32 and stores the raw digest word."""

    dest_var = _next_tmp("crc_dest")
    saved_var = _next_tmp("crc_saved")
    return [
        f"setexpr.l {dest_var} {_format_number(scratch)}",
        f"setexpr.l {saved_var} *${{{dest_var}}}",
        f"crc32 {_format_number(addr)} {_format_number(length)} ${{{dest_var}}}",
        f"setexpr.l {result} *${{{dest_var}}}",
        f"mw.l ${{{dest_var}}} ${{{saved_var}}} 1",
        f"setenv {saved_var}",
        f"setenv {dest_var}",
    ]

def _next_tmp(kind: str) -> str:
    return f"_{next(_TMP_COUNTER)}"


def _normalize_base(tftp: Any, base: str | None) -> str:
    if base is None:
        return str(tftp.rambase)
    if base.startswith("${") and base.endswith("}"):
        return base
    if _IDENT_RE.match(base):
        return f"${{{base}}}"
    return base


def _format_number(value: int | str) -> str:
    if isinstance(value, int):
        return hex(value)
    return value
