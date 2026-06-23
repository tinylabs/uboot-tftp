#!/usr/bin/env python3
"""Example handler module for openipc-tftp."""

from __future__ import annotations
from openipc_tftp.scripted import ReceiveFailedError
from openipc_tftp.ubootscript import *
import re

def uboot_msg(msg: str, clear: bool=False) -> str:
    if clear:
        clr = '\033[2J'
    else:
        clr = ''
    return 'echo ' + clr + '\033[1\;32m' + msg + '\033[0m'

def uboot_err(msg: str) -> str:
    return 'echo ' + '\033[1\;31mError: ' + msg + '\033[0m'

async def nor_backup (tftp, sz: int) -> bytes:
    script = [
        uboot_msg ("Creating backup of NOR flash...", clear=True),
        uboot_memset (tftp, offset=0, size=sz, value=0xFF),
        uboot_nor_read (tftp, ram_offset=0, nor_offset=0, size=sz),
    ]
    return await tftp.exec_recv(script=script, size=sz)

def check_install_args (ip: str, ident: str, cmd: str, base: str, env: dict[str, str]) -> list:
    script = []
    if 'nor' not in env or not bool(re.fullmatch(r"\d+[Mm]", env['nor'])):
        script.append (uboot_err ("Must pass nor=<size>M"))
    if 'vendor' not in env:
        script.append (uboot_err ("Must pass vendor=name"))
    if 'soc' not in env:
        script.append (uboot_err ("Must pass soc=name"))
    if script:
        script.append (uboot_err (f"ie: tftpboot {base} {ip}:id={ident}/{cmd}/vendor=goke/soc=gk7205v300/nor=16M\; source {base}"))
    return script

# Add automatic download of file + caching
# https://openipc.org/cameras/vendors/goke/socs/gk7205v300/download_full_image?flash_size=8&flash_type=nor&fw_release=lite
async def default(tftp, ident: str, cmd: str, env: dict[str, str]):
    if cmd != "install":
        await tftp.exec([uboot_err (f"unknown cmd [{cmd}]")], final=True)
        return

    # Fetch and merge environment
    env = env | await tftp.fetch_env(upload_script=[uboot_msg ("Fetching env...", clear=True)])
    print (env)

    # Check env if we have everything we need
    error = check_install_args(tftp.server_ip, ident, cmd, tftp.rambase, env)
    print (error)
    if error:
        await tftp.exec (error, final=True)
        return
    else:
        sz = int(env["nor"].upper().replace("M", "")) * 1024 * 1024

    # Backup NOR memory
    backup = await nor_backup (tftp, sz)
    tftp.write_file (f'uploads/{ident}-{env["soc"]}-nor-{env["nor"]}.bin', backup)

    # Download firmware, patch mtdparts based on flash sz, copy to static files

    # Flash new firmware

    # Check default MAC and write new one to environment
    await tftp.exec([
        uboot_msg(f"Install complete for {ident}!", clear=True),
        uboot_msg(f"WebUI: http://{env['ipaddr']}/"),
        uboot_msg("Support OpenIPC: https://opencollective.com/openipc/contribute"),
        uboot_msg("Rebooting in 20 seconds..."),
        "sleep 20; reset"
    ], final=True)
