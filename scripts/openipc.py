#!/usr/bin/env python3
"""
Example handler module for uboot-tftp.
Implements installing openipc on ip cameras
"""

from __future__ import annotations

import re
import random
from io import BytesIO
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import build_opener, HTTPCookieProcessor, Request
from http.cookiejar import CookieJar

from uboot_tftp.ubootscript import *
from uboot_tftp.ubootterm import *
from uboot_tftp.ubootenv import *


###
### MOVE BELOW TO FRAMEWORK
###

async def uboot_download_with_progress(tftp, dl_url: str, page_url: str=None, size: int=0) -> bytes:
    cookies = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookies))

    # First request establishes the session cookie.
    if page_url:
        page_req = Request(
            page_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with opener.open(page_req, timeout=30) as r:
            r.read()

    req = Request(
        dl_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/octet-stream,*/*",
            "Referer": page_url,
            # Do not request br/zstd unless you decode them yourself.
            "Accept-Encoding": "identity",
        },
    )

    # Remove request if no referrer
    if not page_url:
        del req.headers['Referrer']
        
    buf = BytesIO()

    with opener.open(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length", "0")) or None
        if total is None and size:
            total = size
        done = 0

        while done < total:
            chunk = r.read(1 * 1024 * 1024)
            if not chunk:
                break

            buf.write(chunk)
            done += len(chunk)

            if done < total:
                pct = int(done / total * 10)
                msg = uboot_progress (int(pct), 10)
            else:
                msg = uboot_msg ("Done")
            await tftp.exec([msg])
    return buf.getvalue()

# Delay then run commands with a chance for the user to break w/ CTRL+c
async def uboot_exec_delay(tftp, msg: str, secs: int, cmds: list, final: bool=False):
    msg = [
        uboot_msg(msg, color='white'),
        uboot_msg("Enter Ctrl+C to cancel...", color='white'),
    ]
    # This isn't really seconds based but close enough on a normal LAN
    for _ in range (secs):
        if not _:
            await tftp.exec([*msg, uboot_progress (_, secs)])
        else:
            await tftp.exec([uboot_progress (_, secs)])
    await tftp.exec([
        *cmds
    ], final=final)

# Back NOR flash to TFTP server
async def uboot_nor_download (tftp, sz: int, msg: str='', final=False) -> bytes:
    script = [uboot_msg(f'{msg}')] if msg else []
    script += [
        uboot_msg ("Copying NOR to RAM... ", bold=True, nl=False),
        uboot_memset (tftp, offset=0, size=sz, value=0xFF),
        uboot_nor_read (tftp, ram_offset=0, nor_offset=0, size=sz),
        uboot_msg ("OK"),
        uboot_msg ("Downloading image via TFTP...", bold=True),
    ]
    return await tftp.exec_recv(script=script, size=sz, final=final)

async def uboot_nor_probe(tftp,
                          env: dict[str, str],
                          max_size=None,
                          final=False) -> int:
    if max_size:
        s = max_size
        max_size = int(s[:-1]) * 2**20 if s[-1].upper() == "M" else None
    if not max_size:
        max_size = int(128*2**20)
    await tftp.exec ([
        uboot_msg("Probing NOR flash... ", nl=False, bold=True),
        'sf probe 0',
        'setenv status $?',
    ], keys=['status'])
    if env['status'] == '1':
        return 0
    await tftp.exec ([
        *uboot_nor_gen_probe(tftp, 2**20, max_size),
        uboot_msg ('${size}')
    ], keys=['size'], final=final)
    return int (env['size'], 0)

async def uboot_nomatch(tftp, ident: str, cmd: str, cmd_list: list=None, final: bool=False) -> None:
    ''' Throw error for no matching entry '''

    cmds = str (cmd_list) if cmd_list else ''
    await tftp.exec ([
        uboot_err(f"uboot-tftp: No matching entry for: id={ident}"),
        uboot_err(f"uboot-tftp: cmd={cmd} is not recognized."),
        uboot_msg(f"uboot-tftp: valid cmds = {cmd_list}"),        
        uboot_msg(f"Add snippet to uboot-tftp config.toml:", color="yellow"),
        uboot_msg(f"[{ident}]", color="yellow"),
        uboot_msg(f"function=<python function name>", color="yellow"),
        uboot_msg()
    ], final=final)

async def uboot_boot(tftp, delay: int=0):
    ''' function boot: Boot device with optional delay '''

    await uboot_exec_delay(tftp, f"Booting in {delay}s", delay, [
        uboot_msg("uboot-tftp: Executing normal boot..."),
        'boot'
    ], final=True)

###
### MOVE ABOVE TO FRAMEWORK
###
        
async def openipc_download_binary(tftp, vendor: str, soc: str, size_mb: int, fw: str) -> bytes:
    page_url = f"https://openipc.org/cameras/vendors/{quote(vendor)}/socs/{quote(soc)}"    
    dl_url = (
        f"https://openipc.org/cameras/vendors/{quote(vendor)}/"
        f"socs/{quote(soc)}/download_full_image"
        f"?flash_size={quote(str(size_mb))}&flash_type=nor&fw_release={quote(fw)}"
    )
    return await uboot_download_with_progress (tftp, dl_url, page_url, int(size_mb*1024*1024))

async def openipc_nor_backup (tftp, sz: int, filename: str='', final=False) -> bytes:
    if not filename:
        filename = f"snapshot-{datetime.now():%Y%m%d-%H%M%S}.bin"
    binary = await uboot_nor_download (tftp, sz)
    filename = f'backup/{filename}'
    tftp.write_file (filename, binary)
    await tftp.exec([
        uboot_msg (f'  Saved backup as {filename}')
    ], final=final)

async def openipc_nor_restore (tftp, filename: str, sz: int):
    script = [
        uboot_msg (f"Uploading {Path(filename).name}... ", nl=False, bold=True),
        uboot_fetch_static (tftp, filename, offset=1024),
        uboot_msg ("OK"),
        uboot_msg ("Erasing flash... ", nl=False, bold=True),
        uboot_nor_erase (offset=0, size=sz),
        uboot_msg ("OK"),
        uboot_msg ("Writing flash... ", nl=False, bold=True),
        uboot_nor_write (tftp, nor_offset=0, ram_offset=1024, size=sz),
        uboot_msg ("OK"),
    ]
    await tftp.exec (script)

def build_runcmd(cmd: str, args: str=''):
    args = f"args={args}" if args else ""
    return '; '.join([
        f"cmd={cmd}",
        f"{args}",
        "run bootstrap"
    ])

def openipc_patch_env(tftp, ident: str, old_env: dict[str,str], new_env: dict[str,str]):
    msgs = []
    if 'ethaddr' in old_env and old_env['ethaddr'] != '00:00:23:34:45:66':
        msgs += [uboot_msg(f"  Reusing ethaddr from old env")]
        new_env['ethaddr'] = old_env['ethaddr']                                     
    elif 'ethaddr' not in new_env or new_env['ethaddr'] == '00:00:23:34:45:66':
        msgs += [uboot_msg(f"  Invalid ethaddr, generating random mac...")]
        mac_bytes = [0x02] + [random.randint(0x00, 0xFF) for _ in range(5)]
        mac = ":".join(f"{b:02x}" for b in mac_bytes)
        new_env['ethaddr'] = mac

    new_env['netinit'] = '; '.join ([
        'if test "${ip}" = "static" || test -n "$netdone" && test "$netdone" -eq 1',
        'then echo "Networking OK"',
        'else setenv autoload no',
        'dhcp',
        'netdone=1',
        'fi'
    ])
    new_env['bootstrap'] = '; '.join ([
        'run netinit',
        f'if tftpboot {tftp.rambase} '+'${serverip}:id=${hostname}/${cmd}/${args}',
        f'then source {tftp.rambase}',
        'else echo "TFTP request failed: is TFTP server running?"',
        'fi'
    ])

    # Set core identifiers
    new_env['bootp_vci'] = f'uboot.{ident}'
    new_env['hostname']  = ident

    # Add a few helper commands
    new_env['install']   = build_runcmd ('install')
    new_env['backup']    = build_runcmd ('backup')
    new_env['probe_nor'] = build_runcmd ('probe')

    # Copy key vars from old to new environment
    keys = ['ipaddr', 'netmask', 'gatewayip', 'dnsip', 'serverip', 'fw', 'ip']
    new_env.update({k: old_env[k] for k in keys if k in old_env})
    for key in ['ethaddr', 'hostname', 'bootp_vci', 'serverip',
                'ipaddr', 'netmask', 'gatewayip', 'fw', 'ip']:
        msgs += [uboot_msg(f"  {key:<10} = {new_env[key]}")]
    return msgs

def check_install_args (tftp, ident: str, cmd: str,
                        env: dict[str, str]) -> list:
    script = []
    if 'vendor' not in env:
        script.append (uboot_err ("Must pass vendor=name"))
    if 'soc' not in env:
        script.append (uboot_err ("Must pass soc=name"))
    if env['fw'] not in ('lite', 'ultimate'):
        script.append (uboot_err (f"Invalid: fw={fw} - Only fw=lite|ultimate supported"))
    if script:
        script.append (uboot_err (f"ie: {tftp.cmdtftp} {tftp.rambase} " +
                                  "{tftp.server_ip}:id={ident}/{cmd}/vendor=goke/soc=gk7205v300/fw=lite; " +
                                  "source {tftp.rambase}"))
    return script

async def openipc_install(tftp, ident: str, cmd: str, tftp_env: dict[str, str]):
    '''
    function: openipc_install - Fully automated openipc install to NOR flash.
    '''

    # Fetch current environment
    cenv = await tftp.fetch_env(
        upload_script=[
            uboot_msg ("Fetching current uboot environment... ", nl=False, bold=True),
        ]
    )
    await tftp.exec([uboot_msg ('OK')])

    # Merge keys from tftp environment (override) if present
    keys = ['nor_size', 'fw', 'vendor', 'soc']
    cenv.update({k: tftp_env[k] for k in keys if k in tftp_env})

    # Set defaults if not present
    cenv.setdefault ('fw', 'lite')
    cenv.setdefault ('ip', 'dhcp')
    cenv.setdefault ('nor_size', None)
    
    # Probe NOR flash
    nor_size = await uboot_nor_probe (tftp, tftp_env, cenv['nor_size'])
    nor_size_mb = int(nor_size / 2**20)
    
    # Check if we have everything we need in env
    msgs = check_install_args(tftp, ident, cmd, cenv)

    # Validate NOR requirements
    if nor_size == 0:
        msgs += [uboot_err("NOR flash not detected! Aborting...")]
    elif nor_size_mb not in [8, 16]:
        msgs += [uboot_err("Only 8M or 16M NOR flash supported.")]
    elif nor_size_mb < 16 and cenv['fw'] == 'ultimate':
        msg += [uboot_err("fw=ultimate requires 16M flash")]
    if msgs:
        await tftp.exec(msgs, final=True)
        return

    # Collect environment variables
    fw = cenv['fw']
    vendor = cenv["vendor"]
    soc = cenv["soc"]
    filename = f"install/openipc-{soc}-{fw}-{nor_size_mb}mb.bin"
    backup_filename = f'install-backup-{ident}-{soc}-{nor_size_mb}mb-{datetime.now():%Y%m%d-%H%M%S}.bin'

    # Backup NOR memory
    await tftp.exec([uboot_msg('Backing up NOR flash.', bold=True)])
    await openipc_nor_backup(tftp, nor_size, backup_filename)

    # Fetch binary for upload
    if tftp.file_exists(filename):
        await tftp.exec ([uboot_msg(f"Using cached openipc binary: {filename}", bold=True)])
        binary = tftp.read_file (filename)
    else:
        await tftp.exec ([uboot_msg("Downloading openipc binary... ", nl=False, bold=True)])
        binary = await openipc_download_binary(tftp, vendor=vendor, soc=soc, fw=fw, size_mb=nor_size_mb)
        if not binary:
            tftp.exec([uboot_err("Failed")], final=True)
            return
        tftp.write_file(filename, binary)

    # Extract uboot env from new image
    await tftp.exec ([uboot_msg("Extracting uboot env from image... ", nl=False, bold=True)])
    try:
        new_env = ubootenv_extract(binary)
    except ValueError as err:
        await tftp.exec ([
            uboot_err(f"Failed to extract uboot env from {Path(filename).name}", final=True),
        ])
        return

    # Patch new environment
    # TODO: check if uboot env crc needs to be big endian on MIPS
    # Otherwise patched env won't load on reset
    msgs = [uboot_msg('OK'), uboot_msg('Patched env variables:', bold=True)] + openipc_patch_env(tftp, ident, cenv, new_env)
    await tftp.exec (msgs)
    patched_bin = ubootenv_patch(binary, new_env)
    filename = f'patched/{ident}-{Path(filename).name}'
    tftp.write_file(filename, patched_bin)

    # TODO:
    # Find partitions in firmware image.
    # Craft mtdparts to match found partitions
    # - Fetch assets from github latest instead
    # https://api.github.com/repos/OpenIPC/firmware/releases/tags/latest
    # uboot, kernel+rootfs
    # Extract partition table from uboot env variables
    # mtdparts=sfc:256k(boot),64k(env),3072k(kernel),10240k(rootfs),-(rootfs_data)
    # Take CRC of each partition to check if we need to reflash
    
    # Flash new firmware
    await openipc_nor_restore (tftp, filename, len (patched_bin))

    # Set new ethaddr and run dhcp for updated IP if applicable
    if new_env['ip'] != 'static' and (new_env['ethaddr'] != cenv['ethaddr']):
        await tftp.exec ([
            uboot_msg(f"Getting new IP with ethaddr={new_env['ethaddr']}..."),
            f'setenv ethaddr {new_env["ethaddr"]}',
            'setenv autoload no',
            'dhcp',
            uboot_msg("Success: ip=${ipaddr} mask=${netmask} gateway=${gatewayip}")
        ], keys=['ipaddr', 'netmask', 'gatewayip'])
    keys = ['ipaddr', 'netmask', 'gatewayip']
    cenv.update({k: tftp_env[k] for k in keys if k in tftp_env})

    # Print complete message
    await tftp.exec([
        uboot_msg(),
        uboot_msg(f"Install finished for {ident}", bold=True),
        uboot_msg(f"------------------------------"),
        uboot_msg(f"Flash backup: {tftp.root}/{backup_filename}"),
        uboot_msg(f"Web UI: http://{cenv['ipaddr']}/"),
        uboot_msg(f"SSH: ssh root@{cenv['ipaddr']} (password: 12345)"),
        uboot_msg("Support OpenIPC: https://opencollective.com/openipc/contribute"),
    ])
    await uboot_exec_delay (tftp, "Rebooting in 10 seconds", 10, ['reset'], final=True)

async def default(tftp, ident: str, cmd: str, tftp_env: dict[str, str]):
    '''
    function: default - Called when config.toml doesn't have matching id=
    declaration.
    '''

    match cmd:
        case 'install':
            await openipc_install (tftp, ident, cmd, tftp_env)
        case 'probe':
            sz = await uboot_nor_probe (tftp, tftp_env, tftp_env.get('nor_size', None), final=True)
        case 'backup':
            sz = await uboot_nor_probe (tftp, tftp_env, tftp_env.get('nor_size', None))
            filename = env.get ('filename', '')
            await openipc_nor_backup(tftp, sz, filename, final=True)            
        case 'boot':
            await uboot_boot (tftp)
        case 'progress':
            for _ in range (10):
                await tftp.exec([uboot_progress(_, 10)])
            await tftp.exec([uboot_msg('Done')], final=True)
                             
        # Unrecognized cmd
        case _:
            await uboot_nomatch(tftp, ident, cmd,
                                cmd_list=['install', 'probe', 'backup', 'boot'])
            await uboot_boot (tftp, delay=10)
