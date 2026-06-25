"""Terminal formatting helpers for U-Boot scripts."""

from __future__ import annotations

# Terminal control
SAVE_CURSOR = "\x1b7"
RESTORE_CURSOR = "\x1b8"
HOME_CURSOR = "\x1b[H"
CLEAR_REGION = "\x1b[J"
CLEAR_SCREEN = "\x1b[2J"
RESTORE = "\x1b[0m"
TERM_RESET = "\x1bc"

# Colors
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
WHITE = "\x1b[37m"

_BOLD_PREFIX = "1\\;"


def uboot_term_reset() -> str:
    """Return a U-Boot command that clears and resets the terminal message area."""

    return f'echo "{CLEAR_SCREEN}{RESTORE}{HOME_CURSOR}{SAVE_CURSOR}"'


def uboot_msg(msg: str = "", color: str = GREEN, bold: bool = False) -> str:
    """Return a U-Boot command that prints a formatted status message."""

    return (
        f"echo {RESTORE_CURSOR}{CLEAR_REGION}"
        f"{_style(color, bold)}{msg}{RESTORE}; echo {SAVE_CURSOR}"
    )


def uboot_err(msg: str, color: str = RED, bold: bool = True) -> str:
    """Return a U-Boot command that prints a formatted error message."""

    return uboot_msg(msg, color=color, bold=bold)


def _style(color: str, bold: bool) -> str:
    if not bold:
        return color
    if not color.startswith("\x1b["):
        return color
    return f"{color[:2]}{_BOLD_PREFIX}{color[2:]}"


__all__ = [
    "CLEAR_REGION",
    "CLEAR_SCREEN",
    "CYAN",
    "GREEN",
    "HOME_CURSOR",
    "RED",
    "RESTORE",
    "RESTORE_CURSOR",
    "SAVE_CURSOR",
    "TERM_RESET",
    "WHITE",
    "YELLOW",
    "uboot_err",
    "uboot_msg",
    "uboot_term_reset",
]
