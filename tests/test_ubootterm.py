from openipc_tftp.ubootterm import (
    GREEN,
    YELLOW,
    uboot_err,
    uboot_msg,
    uboot_msg_reset,
)


def test_uboot_msg_reset_clears_terminal_message_area():
    assert uboot_msg_reset() == 'echo "\x1b[2J\x1b[0m\x1b[H\x1b7"'


def test_uboot_msg_formats_status_message():
    assert uboot_msg("hello") == "echo \x1b8\x1b[J\x1b[32mhello\x1b[0m; echo \x1b7"


def test_uboot_msg_can_apply_bold_color():
    assert (
        uboot_msg("hello", color=YELLOW, bold=True)
        == "echo \x1b8\x1b[J\x1b[1\\;33mhello\x1b[0m; echo \x1b7"
    )


def test_uboot_err_defaults_to_bold_red_message():
    assert (
        uboot_err("boom")
        == "echo \x1b8\x1b[J\x1b[1\\;31mboom\x1b[0m; echo \x1b7"
    )


def test_uboot_err_allows_style_override():
    assert (
        uboot_err("warn", color=GREEN, bold=False)
        == "echo \x1b8\x1b[J\x1b[32mwarn\x1b[0m; echo \x1b7"
    )
