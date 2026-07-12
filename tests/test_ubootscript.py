import re

from uboot_tftp.ubootscript import (
    uboot_crc32_gen,
    uboot_fetch_static,
    uboot_memcpy,
    uboot_memset,
    uboot_nor_erase,
    uboot_nor_read,
    uboot_nor_write,
)


class FakeTftp:
    rambase = "${loadaddr}"
    server_ip = "127.0.0.1"


def test_uboot_memset_uses_session_rambase_and_unsets_tmp():
    script = uboot_memset(FakeTftp(), 0x100, 0xFF, 0x20)

    lines = script.splitlines()
    tmp_name = lines[0].split()[1]
    assert re.fullmatch(r"t[0-9]+", tmp_name)
    assert lines[0] == f"setexpr {tmp_name} ${{loadaddr}} + 0x100"
    assert lines[1] == f"mw.b ${{{tmp_name}}} 0xff 0x20"
    assert lines[2] == f"setenv {tmp_name}"


def test_uboot_memcpy_uses_override_base_and_unsets_tmps():
    script = uboot_memcpy(FakeTftp(), 0x200, 0x100, 0x40, base="baseaddr")

    lines = script.splitlines()
    src_tmp = lines[0].split()[1]
    dst_tmp = lines[1].split()[1]
    assert re.fullmatch(r"t[0-9]+", src_tmp)
    assert re.fullmatch(r"t[0-9]+", dst_tmp)
    assert src_tmp != dst_tmp
    assert lines[0] == f"setexpr {src_tmp} ${{baseaddr}} + 0x100"
    assert lines[1] == f"setexpr {dst_tmp} ${{baseaddr}} + 0x200"
    assert lines[2] == f"cp.b ${{{src_tmp}}} ${{{dst_tmp}}} 0x40"
    assert lines[3] == f"setenv {src_tmp}"
    assert lines[4] == f"setenv {dst_tmp}"


def test_uboot_memset_accepts_absolute_base_literal():
    script = uboot_memset(FakeTftp(), "0x10", "0xff", "0x20", base="0x82000000")

    assert "setexpr " in script
    assert " 0x82000000 + 0x10" in script


def test_uboot_nor_erase_emits_probe_and_erase():
    script = uboot_nor_erase(0x10000, 0x2000)

    assert script.splitlines() == [
        "sf probe 0",
        "sf erase 0x10000 0x2000",
    ]


def test_uboot_nor_read_uses_relative_ram_offset_and_unsets_tmp():
    script = uboot_nor_read(FakeTftp(), 0x400, 0x10000, 0x2000)

    lines = script.splitlines()
    assert lines[0] == "sf probe 0"
    tmp_name = lines[1].split()[1]
    assert re.fullmatch(r"t[0-9]+", tmp_name)
    assert lines[1] == f"setexpr {tmp_name} ${{loadaddr}} + 0x400"
    assert lines[2] == f"sf read ${{{tmp_name}}} 0x10000 0x2000"
    assert lines[3] == f"setenv {tmp_name}"


def test_uboot_nor_write_uses_relative_ram_offset_and_unsets_tmp():
    script = uboot_nor_write(FakeTftp(), 0x20000, 0x800, 0x1000)

    lines = script.splitlines()
    assert lines[0] == "sf probe 0"
    tmp_name = lines[1].split()[1]
    assert re.fullmatch(r"t[0-9]+", tmp_name)
    assert lines[1] == f"setexpr {tmp_name} ${{loadaddr}} + 0x800"
    assert lines[2] == f"sf write ${{{tmp_name}}} 0x20000 0x1000"
    assert lines[3] == f"setenv {tmp_name}"


def test_uboot_fetch_static_uses_session_rambase_by_default():
    script = uboot_fetch_static(FakeTftp(), "images/fw.bin")

    assert script == 'tftpboot ${loadaddr} "127.0.0.1:images/fw.bin"'


def test_uboot_fetch_static_uses_relative_offset_and_unsets_tmp():
    script = uboot_fetch_static(FakeTftp(), "/images/fw.bin", offset=0x400)

    lines = script.splitlines()
    tmp_name = lines[0].split()[1]
    assert re.fullmatch(r"t[0-9]+", tmp_name)
    assert lines[0] == f"setexpr {tmp_name} ${{loadaddr}} + 0x400"
    assert lines[1] == f'tftpboot ${{{tmp_name}}} "127.0.0.1:images/fw.bin"'
    assert lines[2] == f"setenv {tmp_name}"


def test_uboot_crc32_gen_saves_and_restores_scratch_word():
    script = uboot_crc32_gen(0x42000000, 0x1000, scratch="${loadaddr}", result="crc_out")

    lines = script
    dest_tmp = lines[0].split()[1]
    saved_tmp = lines[1].split()[1]
    assert re.fullmatch(r"t[0-9]+", dest_tmp)
    assert re.fullmatch(r"t[0-9]+", saved_tmp)
    assert dest_tmp != saved_tmp
    assert lines[0] == f"setexpr.l {dest_tmp} ${{loadaddr}}"
    assert lines[1] == f"setexpr.l {saved_tmp} *${{{dest_tmp}}}"
    assert lines[2] == f"crc32 0x42000000 0x1000 ${{{dest_tmp}}}"
    assert lines[3] == f"setexpr.l crc_out *${{{dest_tmp}}}"
    assert lines[4] == f"mw.l ${{{dest_tmp}}} ${{{saved_tmp}}} 1"
    assert lines[5] == f"setenv {saved_tmp}"
    assert lines[6] == f"setenv {dest_tmp}"
