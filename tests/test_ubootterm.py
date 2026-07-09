from uboot_tftp.ubootterm import (
    ANSI_COLORS,
    uboot_err,
    uboot_msg,
    uboot_progress,
    uboot_term_reset,
)


def test_uboot_term_reset_clears_terminal_message_area():
    assert uboot_term_reset() == 'echo "\x1b[2J\x1b[0m\x1b[H\x1b7"'


def test_uboot_msg_formats_status_message():
    assert (
        uboot_msg("hello")
        == 'echo "\x1b8\x1b[J\x1b[32mhello\x1b[0m"; echo "\x1b7"'
    )


def test_uboot_progress_draws_saved_line_progress_bar():
    assert uboot_progress(3, 10) == 'echo "\x1b8\x1b[J\x1b7\x1b[32m[###       ]\x1b[0m"'


def test_uboot_progress_clamps_to_bar_width():
    assert uboot_progress(12, 10) == 'echo "\x1b8\x1b[J\x1b7\x1b[32m[##########]\x1b[0m"'


def test_uboot_progress_clamps_negative_values():
    assert uboot_progress(-1, 4) == 'echo "\x1b8\x1b[J\x1b7\x1b[32m[    ]\x1b[0m"'


def test_uboot_msg_can_apply_bold_color():
    assert (
        uboot_msg("hello", color="yellow", bold=True)
        == 'echo "\x1b8\x1b[J\x1b[1;33mhello\x1b[0m"; echo "\x1b7"'
    )


def test_uboot_msg_supports_all_basic_ansi_colors():
    expected_codes = {
        "black": 30,
        "red": 31,
        "green": 32,
        "yellow": 33,
        "blue": 34,
        "magenta": 35,
        "cyan": 36,
        "white": 37,
    }

    assert ANSI_COLORS == tuple(expected_codes)
    for color, code in expected_codes.items():
        expected = f'echo "\x1b8\x1b[J\x1b[{code}mhello\x1b[0m"; echo "\x1b7"'
        assert (
            uboot_msg("hello", color=color)
            == expected
        )


def test_uboot_msg_quotes_shell_control_characters_inside_message():
    expected = (
        'echo "\x1b8\x1b[J\x1b[32m'
        'fw=lite|ultimate; say \\"hello\\"\x1b[0m"; echo "\x1b7"'
    )

    assert (
        uboot_msg('fw=lite|ultimate; say "hello"')
        == expected
    )


def test_uboot_err_defaults_to_bold_red_message():
    assert (
        uboot_err("boom")
        == 'echo "\x1b8\x1b[J\x1b[1;31mboom\x1b[0m"; echo "\x1b7"'
    )


def test_uboot_err_allows_style_override():
    assert (
        uboot_err("warn", color="green", bold=False)
        == 'echo "\x1b8\x1b[J\x1b[32mwarn\x1b[0m"; echo "\x1b7"'
    )


def test_uboot_msg_rejects_unknown_color():
    try:
        uboot_msg("hello", color="orange")
    except ValueError as error:
        assert str(error) == "unsupported ANSI color: 'orange'"
    else:
        raise AssertionError("expected ValueError")
