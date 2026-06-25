#!/usr/bin/env python3
"""Example handler module for openipc-tftp."""

from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import quote
from urllib.request import urlopen

from openipc_tftp.ubootterm import *
from openipc_tftp.ubootscript import *

# Delay then run commands with a chance to break
async def uboot_exec_delay(tftp, msg: str, secs: int, cmds: list, final: boot=False):
    await tftp.exec([
        f'echo "{RESTORE_CURSOR}{CLEAR_REGION}\\c"',
        f'echo "{msg}"',
        'echo ""',
        'echo "Enter Ctrl+C to cancel..."',
        f'echo {SAVE_CURSOR}'
    ])
    for _ in range (secs):
        await tftp.exec([
            f'echo "{RESTORE_CURSOR}{CLEAR_REGION}{SAVE_CURSOR}Executing in: {secs - _}s"',
            #'sleep 0.1'
        ])
    await tftp.exec([
        *cmds
    ], final=final)

# Download official openipc binary
def download_openipc_binary(vendor: str, soc: str, size: str, fw: str) -> bytes:
    url = (
        f"https://openipc.org/cameras/vendors/{quote(vendor)}/"
        f"socs/{quote(soc)}/download_full_image"
        f"?flash_size={quote(size)}&flash_type=nor&fw_release={quote(fw)}"
    )
    with urlopen(url) as response:
        return response.read()

# Back NOR flash to TFTP server
async def openipc_nor_backup (tftp, sz: int) -> bytes:
    script = [
        uboot_msg ("Creating backup of NOR flash..."),
        uboot_memset (tftp, offset=0, size=sz, value=0xFF),
        uboot_nor_read (tftp, ram_offset=0, nor_offset=0, size=sz),
    ]
    return await tftp.exec_recv(script=script, size=sz)

# TODO: Run scripts from offset to base
# Safer that way to avoid collisions
# Currently we offset by 1k to not interfere with the script
async def openipc_nor_install (tftp, filename: str, sz: int):
    script = [
        uboot_msg (f"Uploading {Path(filename).name}..."),
        uboot_fetch_static (tftp, filename, offset=1024),
        uboot_msg ("Erasing flash..."),
        uboot_nor_erase (offset=0, size=sz),
        uboot_msg ("Writing flash..."),
        uboot_nor_write (tftp, nor_offset=0, ram_offset=1024, size=sz),
        uboot_msg ("Flashing complete."),
    ]
    await tftp.exec (script)

def check_install_args (ip: str, ident: str, cmd: str, fw: str, base: str, env: dict[str, str]) -> list:
    script = []
    if 'nor' not in env or not bool(re.fullmatch(r"\d+[Mm]", env['nor'])):
        script.append (uboot_err ("Must pass nor=<size>M"))
    if 'vendor' not in env:
        script.append (uboot_err ("Must pass vendor=name"))
    if 'soc' not in env:
        script.append (uboot_err ("Must pass soc=name"))
    if fw not in ('lite', 'ultimate'):
        script.append (uboot_err (f"Invalid: fw={fw} - Only fw=lite\\|ultimate supported"))
    if script:
        script.append (uboot_err (f"ie: tftpboot {base} {ip}:id={ident}/{cmd}/vendor=goke/soc=gk7205v300/nor=16M/fw=lite\\; source {base}"))
    return script

async def openipc_install(tftp, ident: str, cmd: str, env: dict[str, str]):
    # Fetch and merge environment
    env = env | await tftp.fetch_env(
        upload_script=[
            uboot_term_reset (),
            uboot_msg ("Fetching uboot environment..."),
        ]
    )
    await tftp.exec ([
        uboot_msg("Merged environment with local env.")
    ])

    # Default to lite firmware if not specified
    fw = env.get ('fw', 'lite')

    # Check env if we have everything we need
    error = check_install_args(tftp.server_ip, ident, cmd, fw, tftp.rambase, env)
    if error:
        await tftp.exec (error, final=True)
        return
    else:
        sz = int(env["nor"].upper().replace("M", "")) * (2 ** 20)
        if sz < 16 * (2 ** 20) and fw == 'ultimate':
            await tftp.exec ([uboot_err("fw=ultimate requires at least 16M flash")], final=True)
            return

    # Backup NOR memory
    backup = await openipc_nor_backup (tftp, sz)
    backup_filename = f'backup/{ident}-{env["soc"]}-nor-{env["nor"]}.bin'
    tftp.write_file (backup_filename, backup)

    # Collect environment variables
    vendor = env["vendor"]
    soc = env["soc"]
    size = env["nor"][:-1]
    filename = f"install/openipc-{soc}-{fw}-{size}mb.bin"
    if tftp.file_exists(filename):
        await tftp.exec ([
            uboot_msg(f"Using cached binary: {Path(filename).name}")
        ])
        binary = tftp.read_file (filename)
    else:
        await tftp.exec ([
            uboot_msg("Downloading binary..."),
        ])
        binary = download_openipc_binary(vendor=vendor, soc=soc, size=size, fw=fw)
        tftp.write_file(filename, binary)

    # TODO:
    # Patch mtdparts based on flash sz before flashing
    # Needed for 16M nor flash
    # patch ethaddr with a random address
    # Store server:backup_path in uboot-env
    # Merge old environment vars if applicable

    # After patching set ethaddr= to our new MAC
    # Run DHCP so we can output a reachable IP in final msg

    # Flash new firmware
    await openipc_nor_install (tftp, filename, len (binary))

    # Print complete message
    await tftp.exec([
        uboot_msg(),
        uboot_msg(f"Install finished for {ident}.", bold=True),
        uboot_msg(f"------------------------------"),
        uboot_msg(f"Flash backup: {tftp.root}/{backup_filename}", bold=True),
        uboot_msg(f"Web UI: http://{env['ipaddr']}/", bold=True),
        uboot_msg(f"SSH: ssh root@{env['ipaddr']} (password: 12345)", bold=True),
        uboot_msg("Support OpenIPC: https://opencollective.com/openipc/contribute", color=YELLOW, bold=True),
    ])
    await uboot_exec_delay (tftp, "Rebooting in 20 seconds...", 20, ['reset'], final=True)

# Just boot camera
async def boot(tftp, ident: str, cmd: str, env: dict[str, str]):
    delay = env.get ('delay', 0)
    if delay:
        await tftp.exec ([
            uboot_term_reset(),
            uboot_err(f"openipc-tftp: No matching entry for: {ident}"),
            uboot_msg(f"Add snippet to openipc-tftp config.toml:", color=YELLOW),
            uboot_msg(f"  [{ident}]", color=YELLOW, bold=True),
            uboot_msg(f"  script=<python function name>", color=YELLOW, bold=True),
        ])
        await uboot_exec_delay(tftp, f"Running normal boot in {delay}s",
                               delay, ['boot'], final=True)
    else:
        await tftp.exec ([
            uboot_term_reset(),
            uboot_msg("openipc-tftp: Executing normal boot..."),
            "boot"
        ])

# Default target when no match in config.toml
async def default(tftp, ident: str, cmd: str, env: dict[str, str]):
    match cmd:
        case 'install':
            await openipc_install (tftp, ident, cmd, env)
        case 'boot':
            await boot (tftp, ident, cmd, env)
        case _:
            env['delay'] = 10
            await boot (tftp, ident, cmd, env)
