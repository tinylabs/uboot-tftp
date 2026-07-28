import asyncio
import re
from types import SimpleNamespace

from uboot_tftp.tools import builtin_flash_restore


class RestoreHandle:
    rambase = "${loadaddr}"
    cmdtftp = "tftpboot"
    server_ip = "192.0.2.1"

    def __init__(self, payload: bytes, flash_size: int) -> None:
        self.payload = payload
        self.env = {
            "__nor_probe_status": "0",
            "__nor_probe_size": hex(flash_size),
        }
        self.exec_calls = []
        self.queued = []

    def read_file(self, path: str) -> bytes:
        assert path == "backup/snapshot.bin"
        return self.payload

    def bind(self, logical_key, *, source_key=None, public=False):
        return SimpleNamespace(
            capture=lambda: source_key,
            int=lambda: int(self.env[logical_key], 0),
        )

    async def exec(self, script, *, requires=(), final=False, **_kwargs):
        self.exec_calls.append(
            {
                "script": list(script),
                "requires": list(requires),
                "final": final,
            }
        )

    def exec_queue(self, script, *, requires=()):
        self.queued.extend(script)


def test_builtin_flash_restore_stages_the_backup_and_only_flashes_after_download():
    handle = RestoreHandle(payload=b"x" * 0x2000, flash_size=0x2000)

    asyncio.run(builtin_flash_restore(handle, "cam123", {"file": "snapshot.bin"}))

    assert len(handle.exec_calls) == 2
    call = handle.exec_calls[1]
    script = "\n".join(call["script"])
    assert call["final"] is False
    download_line = next(line for line in script.splitlines() if "backup/snapshot.bin" in line)
    assert re.fullmatch(
        r'tftpboot \$\{t[0-9]+\} "192\.0\.2\.1:backup/snapshot\.bin"',
        download_line,
    )
    assert "setenv __uboot_tftp_restore_download_status $?" in script
    assert "if test ${__uboot_tftp_restore_download_status} -eq 0; then" in script
    assert script.index("sf erase 0x0 0x2000") > script.index("; then")
    assert script.index("sf write") > script.index("sf erase 0x0 0x2000")
    assert "Failed to download backup/snapshot.bin; flash was not modified." in script
    assert "setenv __uboot_tftp_restore_download_status" in script
