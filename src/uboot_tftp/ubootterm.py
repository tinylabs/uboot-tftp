"""Terminal formatting helpers for U-Boot scripts."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

ColorName = Literal[
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
]
EchoNoNewline = Literal["backslash_c", "dash_n"]

_echo_no_newline: ContextVar[EchoNoNewline] = ContextVar(
    "echo_no_newline", default="backslash_c"
)

# Terminal control
SAVE_CURSOR = "\x1b7"
RESTORE_CURSOR = "\x1b8"
HOME_CURSOR = "\x1b[H"
CLEAR_REGION = "\x1b[J"
CLEAR_SCREEN = "\x1b[2J"
RESTORE = "\x1b[0m"
TERM_RESET = "\x1bc"

ANSI_COLORS: tuple[ColorName, ...] = (
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
)
_COLOR_CODES: dict[ColorName, int] = {
    "black": 30,
    "red": 31,
    "green": 32,
    "yellow": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
}


def uboot_term_reset() -> str:
    """Return a U-Boot command that clears and resets the terminal message area."""

    return _echo(f"{CLEAR_SCREEN}{RESTORE}{HOME_CURSOR}{SAVE_CURSOR}")


def uboot_status(msg, color: ColorName = "green", bold: bool = False) -> str:
    """Return a U-Boot command that redraws the saved-line progress indicator."""

    return _echo(f"{RESTORE_CURSOR}{CLEAR_REGION}{SAVE_CURSOR}{_style(color, bold)}{msg}{RESTORE}")

def uboot_status_complete():
    """ Saves cursor so status isn't overwritten """

    return _echo(f"{SAVE_CURSOR}")

def uboot_progress(x: int, total: int, color: ColorName = "green", bold: bool = False) -> str:
    """Return a U-Boot command that redraws the saved-line progress indicator."""

    width = max(int(total), 0)
    filled = min(max(int(x), 0), width)
    icon = "[" + ("#" * filled) + (" " * (width - filled)) + "]"
    return uboot_status(icon, color, bold)

def uboot_msg(
    msg: str = "", color: ColorName = "green", bold: bool = False, nl: bool = True
) -> str:
    """Return a U-Boot command that prints a formatted status message."""

    value = f"{RESTORE_CURSOR}{CLEAR_REGION}{_style(color, bold)}{msg}{RESTORE}"
    command = _echo(value) if nl else _echo_without_newline(value)
    return (
        command + f"; {_echo(SAVE_CURSOR)}"
    )

def _echo(value: str) -> str:
    return f"echo {_quote(value)}"

def _echo_without_newline(value: str) -> str:
    if _echo_no_newline.get() == "backslash_c":
        return _echo(f"{value}\\c")
    return f"echo -n {_quote(value)}"


@contextmanager
def echo_no_newline_mode(mode: EchoNoNewline):
    """Temporarily select the U-Boot echo convention used for ``nl=False``."""

    token = _echo_no_newline.set(mode)
    try:
        yield
    finally:
        _echo_no_newline.reset(token)

def _quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'

def uboot_err(msg: str, color: ColorName = "red", bold: bool = True) -> str:
    """Return a U-Boot command that prints a formatted error message."""

    return uboot_msg(msg, color=color, bold=bold)


def _style(color: ColorName, bold: bool) -> str:
    try:
        color_code = _COLOR_CODES[color]
    except KeyError:
        raise ValueError(f"unsupported ANSI color: {color!r}") from None
    params = [str(color_code)]
    if bold:
        params.insert(0, "1")
    return f"\x1b[{';'.join(params)}m"


__all__ = [
    "ANSI_COLORS",
    "CLEAR_REGION",
    "CLEAR_SCREEN",
    "ColorName",
    "EchoNoNewline",
    "HOME_CURSOR",
    "RESTORE",
    "RESTORE_CURSOR",
    "SAVE_CURSOR",
    "TERM_RESET",
    "echo_no_newline_mode",
    "uboot_err",
    "uboot_msg",
    "uboot_progress",
    "uboot_status",
    "uboot_status_complete",
    "uboot_term_reset",
]
