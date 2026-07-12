import logging
import asyncio
from types import SimpleNamespace

from uboot_tftp.ubootops import (
    uboot_boot,
    uboot_crc32,
    uboot_exec_delay,
    uboot_nor_download,
    uboot_nor_probe,
)


class FakeHandle:
    def __init__(self, env=None):
        self.rambase = "${loadaddr}"
        self.is_le = True
        self.ident = "fake"
        self.env = {} if env is None else dict(env)
        self.session = SimpleNamespace(env=self.env)
        self.exec_calls = []
        self.exec_recv_calls = []

    async def exec(self, script, *, final=False, keys=(), returns=(), requires=()):
        self.exec_calls.append(
            {
                "script": list(script),
                "final": final,
                "keys": list(keys),
                "returns": list(returns),
                "requires": list(requires),
            }
        )

    async def exec_recv(
        self,
        script,
        size,
        *,
        final=False,
        keys=(),
        returns=(),
        offset=None,
        requires=(),
    ):
        self.exec_recv_calls.append(
            {
                "script": list(script),
                "size": size,
                "final": final,
                "keys": list(keys),
                "returns": list(returns),
                "offset": offset,
                "requires": list(requires),
            }
        )
        return b"payload"

    def bind(
        self,
        logical_key,
        *,
        source_key=None,
        public=False,
    ):
        return SimpleNamespace(
            _env=self.env,
            logical_key=logical_key,
            source_key=source_key,
            public=public,
            capture=lambda: source_key,
            str=lambda: self.env[logical_key],
            int=lambda: int(self.env[logical_key], 0),
        )


def test_uboot_nor_download_builds_script_around_core_nor_commands():
    handle = FakeHandle()

    result = asyncio.run(
        uboot_nor_download(
            handle,
            0x2000,
            pre_cmds=["echo before"],
            post_cmds=["echo after"],
        )
    )

    assert result == b"payload"
    assert len(handle.exec_recv_calls) == 1
    call = handle.exec_recv_calls[0]
    assert call["size"] == 0x2000
    assert call["script"][0] == "echo before"
    assert "mw.b" in call["script"][1]
    assert "sf read" in call["script"][2]
    assert call["script"][3] == "echo after"


def test_uboot_nor_probe_returns_zero_when_sf_probe_fails():
    handle = FakeHandle(env={"__nor_probe_status": "1", "__nor_probe_size": "0"})

    result = asyncio.run(
        uboot_nor_probe(
            handle,
            pre_cmds=["echo before"],
            post_cmds=["echo after"],
        )
    )

    assert result == 0
    assert len(handle.exec_calls) == 1
    call = handle.exec_calls[0]
    assert call["script"][0] == "echo before"
    assert call["returns"][0].logical_key == "__nor_probe_status"
    assert call["returns"][1].logical_key == "__nor_probe_size"
    assert "sf probe 0" in call["script"]
    assert any("setenv _r1 0" in line for line in call["script"])


def test_uboot_nor_probe_runs_recursive_probe_and_parses_hex_size():
    handle = FakeHandle(env={"__nor_probe_status": "0", "__nor_probe_size": "0x1000000"})

    result = asyncio.run(
        uboot_nor_probe(
            handle,
            max_size="16M",
            pre_cmds=["echo before"],
            post_cmds=["echo after"],
            final=True,
        )
    )

    assert result == 0x1000000
    assert len(handle.exec_calls) == 1
    call = handle.exec_calls[0]
    assert call["returns"][0].logical_key == "__nor_probe_status"
    assert call["returns"][1].logical_key == "__nor_probe_size"
    assert call["script"][0] == "echo before"
    assert call["final"] is True
    assert "    echo after" in call["script"]
    assert any("setenv _r1 " in line for line in call["script"])
    assert not any(line.strip() == "setenv size" for line in call["script"])
    assert any("sf read" in line for line in call["script"])


def test_uboot_exec_delay_shows_intro_then_runs_commands():
    handle = FakeHandle()

    asyncio.run(
        uboot_exec_delay(
            handle,
            "Booting in 3s",
            3,
            ["boot"],
            final=True,
        )
    )

    assert len(handle.exec_calls) == 4
    first, second, third, fourth = handle.exec_calls
    assert "Booting in 3s" in first["script"][0]
    assert "Enter Ctrl+C to cancel..." in first["script"][1]
    assert first["final"] is False
    assert "[#  ]" in second["script"][0]
    assert "[## ]" in third["script"][0]
    assert fourth["script"] == ["boot"]
    assert fourth["final"] is True


def test_uboot_exec_delay_runs_commands_immediately_for_zero_seconds():
    handle = FakeHandle()

    asyncio.run(uboot_exec_delay(handle, "Now", 0, ["boot"], final=True))

    assert handle.exec_calls == [
        {
            "script": ["boot"],
            "final": True,
            "keys": [],
            "returns": [],
            "requires": [],
        }
    ]


def test_uboot_boot_uses_standard_boot_message_and_command():
    handle = FakeHandle()

    asyncio.run(uboot_boot(handle, delay=1))

    assert len(handle.exec_calls) == 2
    assert "Booting in 1s" in handle.exec_calls[0]["script"][0]
    assert handle.exec_calls[1]["script"][0] != "boot"
    assert "Executing normal boot..." in handle.exec_calls[1]["script"][0]
    assert handle.exec_calls[1]["script"][1] == "boot"
    assert handle.exec_calls[1]["final"] is True


def test_uboot_crc32_builds_script_and_decodes_little_endian_words():
    handle = FakeHandle(env={"c0": "78563412", "c1": "f0debc9a"})

    result = asyncio.run(
        uboot_crc32(
            handle,
            [(0x42000000, 0x1000), (0x43000000, 0x2000)],
            pre_cmds=["echo before"],
            post_cmds=["echo after"],
            final=True,
        )
    )

    assert result == [0x12345678, 0x9ABCDEF0]
    assert len(handle.exec_calls) == 1
    call = handle.exec_calls[0]
    assert call["script"][0] == "echo before"
    assert call["script"][-1] == "echo after"
    assert [ret.logical_key for ret in call["returns"]] == ["c0", "c1"]
    assert call["final"] is True
    assert any("crc32 0x42000000 0x1000" in line for line in call["script"])
    assert any("crc32 0x43000000 0x2000" in line for line in call["script"])


def test_uboot_crc32_decodes_big_endian_words_when_requested():
    handle = FakeHandle(env={"c0": "12345678"})

    result = asyncio.run(
        uboot_crc32(
            handle,
            [(0x42000000, 0x1000)],
            little_endian=False,
        )
    )

    assert result == [0x12345678]


def test_uboot_crc32_requires_endianness_without_session_preflight():
    handle = FakeHandle(env={"c0": "12345678"})
    handle.is_le = None

    try:
        asyncio.run(uboot_crc32(handle, [(0x42000000, 0x1000)]))
    except ValueError as exc:
        assert "little_endian" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_uboot_crc32_rejects_more_than_six_ranges(caplog):
    handle = FakeHandle()
    caplog.set_level(logging.ERROR)

    try:
        asyncio.run(uboot_crc32(handle, [(0x42000000 + i, 0x1000) for i in range(7)]))
    except ValueError as exc:
        assert "at most 6 ranges" in str(exc)
    else:
        raise AssertionError("expected ValueError")

    assert len(handle.exec_calls) == 1
    assert handle.exec_calls[0]["final"] is True
    assert "at most 6 ranges" in handle.exec_calls[0]["script"][0]
    assert "Rejecting CRC32 request with too many ranges" in caplog.text
