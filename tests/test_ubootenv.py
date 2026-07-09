import gzip

import pytest

from uboot_tftp.ubootenv import (
    EnvPartitionInfo,
    extract_default_env_from_uboot,
    ubootenv_build,
    ubootenv_extract,
    ubootenv_find,
    ubootenv_parse_export,
    ubootenv_parse_part,
    ubootenv_patch,
)

DEFAULT_ENV = {
    "bootargs": "console=ttyAMA0,115200 root=/dev/mtdblock3",
    "bootcmd": "sf probe 0; sf read ${baseaddr} 0x50000 0x300000; bootm ${baseaddr}",
    "baudrate": "115200",
    "ipaddr": "192.168.1.10",
    "serverip": "192.168.1.1",
    "mtdparts": "sfc:256k(boot),64k(env),2048k(kernel),-(rootfs)",
}


def test_parse_env_export_accepts_nul_delimited_content():
    body = b"bootcmd=run boot\x00ethaddr=00:11:22:33:44:55\x00"

    env = ubootenv_parse_export(body)

    assert env == {
        "bootcmd": "run boot",
        "ethaddr": "00:11:22:33:44:55",
    }


def test_parse_env_export_accepts_newline_delimited_content():
    body = b"ipaddr=192.168.1.50\nserverip=192.168.1.1\n"

    env = ubootenv_parse_export(body)

    assert env == {
        "ipaddr": "192.168.1.50",
        "serverip": "192.168.1.1",
    }


def test_parse_env_partition_accepts_crc_prefixed_block():
    partition = _build_env_partition({"bootcmd": "run boot", "serverip": "192.168.1.1"})

    env = ubootenv_parse_part(partition)

    assert env == {
        "bootcmd": "run boot",
        "serverip": "192.168.1.1",
    }


def test_parse_env_partition_accepts_redundant_env_block():
    partition = _build_env_partition(
        {"bootcmd": "run boot", "ipaddr": "192.168.1.50"},
        header_size=5,
    )

    env = ubootenv_parse_part(partition)

    assert env == {
        "bootcmd": "run boot",
        "ipaddr": "192.168.1.50",
    }


def test_parse_env_partition_rejects_erased_blocks():
    with pytest.raises(ValueError, match="erased"):
        ubootenv_parse_part(b"\xff" * 0x10000)


def test_build_env_image_round_trips_minimal_image():
    image = ubootenv_build({"bootcmd": "run boot", "serverip": "192.168.1.1"})

    env = ubootenv_parse_part(image)

    assert env == {
        "bootcmd": "run boot",
        "serverip": "192.168.1.1",
    }


def test_build_env_image_round_trips_sized_redundant_image():
    image = ubootenv_build(
        {"bootcmd": "run boot", "ipaddr": "192.168.1.50"},
        size=0x10000,
        flags=0x01,
    )

    env = ubootenv_parse_part(image)

    assert len(image) == 0x10000
    assert env == {
        "bootcmd": "run boot",
        "ipaddr": "192.168.1.50",
    }


def test_build_env_image_rejects_invalid_key():
    with pytest.raises(ValueError, match="invalid U-Boot env key"):
        ubootenv_build({"bad key": "value"})


def test_build_env_image_rejects_too_small_size():
    with pytest.raises(ValueError, match="size is too small"):
        ubootenv_build({"bootcmd": "run boot"}, size=8)


def test_extract_default_env_from_uboot_prefers_plausible_env_blob():
    boot_region = _build_boot_partition(DEFAULT_ENV, include_noise=True)

    env = extract_default_env_from_uboot(boot_region)

    assert env["bootcmd"] == DEFAULT_ENV["bootcmd"]
    assert env["mtdparts"] == DEFAULT_ENV["mtdparts"]


def test_extract_env_from_flash_image_uses_embedded_default_when_env_is_erased():
    image = _build_flash_image(default_env=DEFAULT_ENV)

    env = ubootenv_extract(
        image,
        boot_size=0x40000,
        env_offset=0x40000,
        env_size=0x10000,
    )

    assert env["bootcmd"] == DEFAULT_ENV["bootcmd"]
    assert env["mtdparts"] == DEFAULT_ENV["mtdparts"]


def test_find_env_partition_returns_explicit_empty_partition_bounds():
    image = _build_flash_image(default_env=DEFAULT_ENV)

    info = ubootenv_find(
        image,
        boot_size=0x40000,
        env_offset=0x40000,
        env_size=0x10000,
    )

    assert info == EnvPartitionInfo(offset=0x40000, size=0x10000)


def test_extract_env_from_flash_image_falls_back_when_env_crc_is_invalid():
    image = _build_flash_image(
        default_env=DEFAULT_ENV,
        env_partition=_build_env_partition({"bootcmd": "override"}, corrupt_crc=True),
    )

    env = ubootenv_extract(
        image,
        boot_size=0x40000,
        env_offset=0x40000,
        env_size=0x10000,
    )

    assert env["bootcmd"] == DEFAULT_ENV["bootcmd"]


def test_extract_env_from_flash_image_prefers_non_empty_env_partition():
    image = _build_flash_image(
        default_env=DEFAULT_ENV,
        env_partition=_build_env_partition(
            {
                "bootcmd": "run custom",
                "serverip": "10.0.0.1",
            }
        ),
    )

    env = ubootenv_extract(
        image,
        boot_size=0x40000,
        env_offset=0x40000,
        env_size=0x10000,
    )

    assert env == {
        "bootcmd": "run custom",
        "serverip": "10.0.0.1",
    }


def test_find_env_partition_infers_layout_from_embedded_mtdparts():
    image = _build_flash_image(default_env=DEFAULT_ENV)

    info = ubootenv_find(image)

    assert info == EnvPartitionInfo(offset=0x40000, size=0x10000)


def test_patch_env_in_flash_image_replaces_explicit_env_partition():
    image = _build_flash_image(default_env=DEFAULT_ENV)

    patched = ubootenv_patch(
        image,
        {"bootcmd": "run custom", "serverip": "10.0.0.1"},
        boot_size=0x40000,
        env_offset=0x40000,
        env_size=0x10000,
    )

    assert patched[:0x40000] == image[:0x40000]
    assert patched[0x50000:] == image[0x50000:]
    assert ubootenv_extract(
        patched,
        boot_size=0x40000,
        env_offset=0x40000,
        env_size=0x10000,
    ) == {
        "bootcmd": "run custom",
        "serverip": "10.0.0.1",
    }


def test_patch_env_in_flash_image_can_infer_partition_bounds():
    image = _build_flash_image(default_env=DEFAULT_ENV)

    patched = ubootenv_patch(
        image,
        {"bootcmd": "run custom", "serverip": "10.0.0.1"},
    )

    assert ubootenv_find(patched) == EnvPartitionInfo(offset=0x40000, size=0x10000)
    assert ubootenv_extract(patched) == {
        "bootcmd": "run custom",
        "serverip": "10.0.0.1",
    }


def test_patch_env_in_flash_image_supports_redundant_env_layout():
    image = _build_flash_image(default_env=DEFAULT_ENV)

    patched = ubootenv_patch(
        image,
        {"bootcmd": "run custom", "ipaddr": "192.168.1.50"},
        boot_size=0x40000,
        env_offset=0x40000,
        env_size=0x10000,
        flags=0x01,
    )

    partition = patched[0x40000:0x50000]
    assert partition[4] == 0x01
    assert ubootenv_parse_part(partition) == {
        "bootcmd": "run custom",
        "ipaddr": "192.168.1.50",
    }


def test_extract_env_from_flash_image_infers_layout_from_embedded_mtdparts():
    image = _build_flash_image(default_env=DEFAULT_ENV)

    env = ubootenv_extract(image)

    assert env["bootcmd"] == DEFAULT_ENV["bootcmd"]
    assert env["mtdparts"] == DEFAULT_ENV["mtdparts"]


def test_extract_env_from_standalone_uboot_image_uses_embedded_default():
    image = _build_boot_partition(DEFAULT_ENV)

    env = ubootenv_extract(image)

    assert env["bootcmd"] == DEFAULT_ENV["bootcmd"]
    assert env["mtdparts"] == DEFAULT_ENV["mtdparts"]


def _build_flash_image(
    *,
    default_env: dict[str, str],
    env_partition: bytes | None = None,
    boot_size: int = 0x40000,
    env_size: int = 0x10000,
) -> bytes:
    boot_region = _build_boot_partition(default_env, boot_size=boot_size)
    if env_partition is None:
        env_partition = b"\xff" * env_size
    kernel = b"\x27\x05\x19\x56" + b"\x00" * 0x20000
    return boot_region + env_partition + kernel


def _build_boot_partition(
    env: dict[str, str],
    *,
    boot_size: int = 0x40000,
    include_noise: bool = False,
) -> bytes:
    noise = (
        b"foo=1\x00bar=2\x00baz=3\x00qux=4\x00quux=5\x00\x00"
        if include_noise
        else b""
    )
    payload = (
        b"/tmp/u-boot/common/env_common.c\x00"
        + noise
        + b"prefix\x00"
        + _encode_env(env)
        + b"suffix\x00nvedit.c\x00"
    )
    compressed = gzip.compress(payload)
    prefix = b"\xea" * 0x1000 + compressed
    assert len(prefix) < boot_size
    return prefix + b"\xff" * (boot_size - len(prefix))


def _build_env_partition(
    env: dict[str, str],
    *,
    size: int = 0x10000,
    header_size: int = 4,
    corrupt_crc: bool = False,
) -> bytes:
    image = ubootenv_build(
        env,
        size=size,
        flags=0x01 if header_size == 5 else None,
    )
    if corrupt_crc:
        crc = int.from_bytes(image[:4], "big") ^ 0xFFFFFFFF
        return crc.to_bytes(4, "big") + image[4:]
    return image


def _encode_env(env: dict[str, str]) -> bytes:
    return b"".join(
        f"{key}={value}".encode() + b"\x00"
        for key, value in env.items()
    ) + b"\x00"
