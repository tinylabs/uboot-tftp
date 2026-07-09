import gzip
import json
import zlib

from uboot_tftp.ubootenv import ubootenv_extract
from uboot_tftp.ubootenv_cli import build_parser, main

DEFAULT_ENV = {
    "bootargs": "console=ttyAMA0,115200 root=/dev/mtdblock3",
    "bootcmd": "sf probe 0; sf read ${baseaddr} 0x50000 0x300000; bootm ${baseaddr}",
    "baudrate": "115200",
    "ipaddr": "192.168.1.10",
    "serverip": "192.168.1.1",
    "mtdparts": "sfc:256k(boot),64k(env),2048k(kernel),-(rootfs)",
}


def test_cli_parses_explicit_partition_arguments(tmp_path, capsys):
    image = _build_flash_image(default_env=DEFAULT_ENV)
    image_path = tmp_path / "flash.bin"
    image_path.write_bytes(image)

    exit_code = main(
        [
            str(image_path),
            "--boot-size",
            "0x40000",
            "--env-offset",
            "0x40000",
            "--env-size",
            "0x10000",
        ]
    )

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert "bootcmd=" in "\n".join(lines)
    assert "mtdparts=sfc:256k(boot),64k(env),2048k(kernel),-(rootfs)" in lines


def test_cli_supports_inferred_layout_and_json_output(tmp_path, capsys):
    image = _build_flash_image(default_env=DEFAULT_ENV)
    image_path = tmp_path / "flash.bin"
    image_path.write_bytes(image)

    exit_code = main([str(image_path), "--format", "json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["bootcmd"] == DEFAULT_ENV["bootcmd"]
    assert payload["mtdparts"] == DEFAULT_ENV["mtdparts"]


def test_cli_extracts_embedded_default_from_standalone_uboot_image(tmp_path, capsys):
    image = _build_boot_partition(DEFAULT_ENV)
    image_path = tmp_path / "u-boot.bin"
    image_path.write_bytes(image)

    exit_code = main([str(image_path)])

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert "bootcmd=" in "\n".join(lines)
    assert "mtdparts=sfc:256k(boot),64k(env),2048k(kernel),-(rootfs)" in lines


def test_cli_prefers_non_empty_env_partition(tmp_path, capsys):
    image = _build_flash_image(
        default_env=DEFAULT_ENV,
        env_partition=_build_env_partition(
            {
                "bootcmd": "run custom",
                "serverip": "10.0.0.1",
            }
        ),
    )
    image_path = tmp_path / "flash.bin"
    image_path.write_bytes(image)

    exit_code = main(
        [
            str(image_path),
            "--boot-size",
            "0x40000",
            "--env-offset",
            "0x40000",
            "--env-size",
            "0x10000",
        ]
    )

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["bootcmd=run custom", "serverip=10.0.0.1"]


def test_build_parser_defaults_to_env_output():
    args = build_parser().parse_args(["flash.bin"])

    assert args.image.name == "flash.bin"
    assert args.boot_offset == 0
    assert args.format == "env"
    assert args.output is None
    assert args.assignments == []


def test_cli_can_patch_image_from_set_arguments(tmp_path):
    image = _build_flash_image(default_env=DEFAULT_ENV)
    image_path = tmp_path / "flash.bin"
    output_path = tmp_path / "patched.bin"
    image_path.write_bytes(image)

    exit_code = main(
        [
            str(image_path),
            "--output",
            str(output_path),
            "--set",
            "bootcmd=run custom",
            "--set",
            "serverip=10.0.0.1",
        ]
    )

    assert exit_code == 0
    assert output_path.exists()
    patched_env = ubootenv_extract(output_path.read_bytes())
    assert patched_env == {
        "bootcmd": "run custom",
        "serverip": "10.0.0.1",
    }


def test_cli_can_patch_image_from_json_file(tmp_path):
    image = _build_flash_image(default_env=DEFAULT_ENV)
    image_path = tmp_path / "flash.bin"
    output_path = tmp_path / "patched.bin"
    env_path = tmp_path / "env.json"
    image_path.write_bytes(image)
    env_path.write_text(
        json.dumps({"bootcmd": "run custom", "serverip": "10.0.0.1"})
    )

    exit_code = main(
        [
            str(image_path),
            "--output",
            str(output_path),
            "--env-json",
            str(env_path),
        ]
    )

    assert exit_code == 0
    patched_env = ubootenv_extract(output_path.read_bytes())
    assert patched_env == {
        "bootcmd": "run custom",
        "serverip": "10.0.0.1",
    }


def test_cli_rejects_patch_mode_without_env_values(tmp_path):
    image = _build_flash_image(default_env=DEFAULT_ENV)
    image_path = tmp_path / "flash.bin"
    output_path = tmp_path / "patched.bin"
    image_path.write_bytes(image)

    try:
        main([str(image_path), "--output", str(output_path)])
    except ValueError as exc:
        assert str(exc) == "patch mode requires at least one --set or --env-json value"
    else:
        raise AssertionError("expected patch mode without values to fail")


def test_cli_rejects_invalid_set_assignment(tmp_path):
    image = _build_flash_image(default_env=DEFAULT_ENV)
    image_path = tmp_path / "flash.bin"
    output_path = tmp_path / "patched.bin"
    image_path.write_bytes(image)

    try:
        main([str(image_path), "--output", str(output_path), "--set", "broken"])
    except ValueError as exc:
        assert str(exc) == "invalid --set assignment: 'broken'"
    else:
        raise AssertionError("expected invalid --set assignment to fail")


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
) -> bytes:
    payload = (
        b"/tmp/u-boot/common/env_common.c\x00"
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
) -> bytes:
    payload = _encode_env(env)
    data_size = size - 4
    assert len(payload) < data_size
    body = payload + b"\xff" * (data_size - len(payload))
    crc = zlib.crc32(body) & 0xFFFFFFFF
    header = crc.to_bytes(4, "big")
    return header + body


def _encode_env(env: dict[str, str]) -> bytes:
    return b"".join(
        f"{key}={value}".encode() + b"\x00"
        for key, value in env.items()
    ) + b"\x00"
