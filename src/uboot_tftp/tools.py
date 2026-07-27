"""Built-in special-command script overrides."""

from __future__ import annotations

import re
import inspect
from datetime import datetime
from pathlib import Path
from .ubootterm import *
from .ubootscript import *
from .ubootops import *
from .ubootenv import *

INTERNAL_VARS = {
    'id' : {
        'var' : '<ident>',
        'help' : ['Unique id used by remote session to identify this device.'],
    },
    'ipmode' : {
        'var' : '<ipmode>',
        'help' : [
            'dhcp   : Run dhcp on first session call (autoload=no)',
            'static : ipaddr, netmask, gatewayip, serverip already setup.'
        ],
    },
    'bootp_vci' : {
        'var' : 'uboot-tftp',
        'help' : [
            'DHCP parameter passed during builtin cmd `dhcp`. Can match against this',
            'field on the DHCP server to populate ' + "'${serverip}'" + '=${serverip}.'
        ],
    },
    'session' : {
        'var': '; '.join ([
            'run netinit',
            f'if tftpboot <rambase> ${{serverip}}:id=${{id}}/${{cmd}}/${{args}}',
            f'then source <rambase>',
            'else echo "TFTP request failed: is TFTP server running @ ${serverip}?"',
            'fi',
            'false',
        ]),
        'help' : [
            'Start dynamic session from uboot on this device',
            '`cmd=<cmd>; args=key1=arg1/key2=arg2; run session`',
        ],
    },    
    'netinit' : {
        'var' : '; '.join ([
            'if test "${ipmode}" = "static" || test -n "$netdone" && test "$netdone" -eq 1',
            'then echo "Networking OK"',
            'else setenv autoload no',
            'dhcp',
            'netdone=1',
            'fi']),
        'help' : [
            'Initialize networking based on env variable `ipmode`.'
        ],
    },
    'onboot' : {
        'var' : '; '.join ([
            'cmd=onboot',
            'if run session',
            'then echo ""',
            'echo "Booting default..."',
            'run bootdefault',
            'fi',
        ]),
        'help' : [
        ],
    },
    'persist' : {
        'var' : '; '.join ([
            'if test -n "${bootdefault}"',
            'then echo "bootdefault already set!"',
            'else setenv bootdefault ${bootcmd}',
            'echo "Copying bootcmd to bootdefault"',
            'fi',
            "setenv bootcmd 'run onboot'",
            'saveenv',
            'echo "Installed persistance"',
            'echo "Run `reset` to test"',
        ]),
        'help' : [
        ],
    },
    'unpersist' : {
        'var' : '; '.join ([
            'setenv bootcmd ${bootdefault}',
            'setenv bootdefault',
            'saveenv',
            'echo "Uninstalled persistance"',
        ]),
        'help' : [
        ],
    }
}


def _session_vars(tftp, ident: str, env: dict[str,str]) -> dict:

    # Defaults if not present
    env.setdefault('ipmode', 'dhcp')

    # Sub for session
    mapping = {
        "<ident>"   : ident,
        "<ipmode>"  : env["ipmode"],
        "<rambase>" : str(tftp.rambase),
    }
    pattern = re.compile("|".join(re.escape(k) for k in mapping))
    return {
        key: {
            **data,
            'var': pattern.sub(lambda match: mapping[match.group(0)], data['var'])
        }
        for key, data in INTERNAL_VARS.items()
    }

def _help_msgs (d: dict, expand: bool=False) -> list[str]:
    return [
        line
        for cmd, data in d.items()
        for line in (
                uboot_msg (f"  {cmd}:", bold=True),
                *((uboot_msg (f"    = `${cmd}`"),) if expand else ()),
                *(uboot_msg (f"    {h}", color='cyan') for h in data.get("help", [])),
        )
    ]

async def cmd_bootstrap (tftp, ident: str, env: dict[str, str]):
    var_dict = _session_vars(tftp, ident, env)
    cmds = [f"setenv {key} '{val['var']}'" for key, val in var_dict.items()]
    msgs = [uboot_msg ('Bootstrap complete.')];
    if env.get ('verbose', '1') == '1':
        msgs += [
            uboot_msg (f'Installed {len(cmds)} env variables: {list(var_dict.keys())}', bold=True),
            uboot_msg("Run `saveenv` to persist across reboot", color='yellow'),
            uboot_msg('Run `cmd=@help; run session` to view commands.', color='yellow'),
            uboot_msg('Run `cmd=@help; args=vars=1; run session` to view variables.', color='yellow'),
        ]
    await tftp.exec(cmds + msgs, final=True)

async def cmd_help (tftp, ident: str, env: dict[str, str], cmd: str=''):
    if 'vars' in env:
        var_dict = _session_vars(tftp, ident, env)
        msgs = _help_msgs (var_dict, expand=True)
    else:
        msgs = _help_msgs(dict(CMDS[cmd])) if cmd else _help_msgs(CMDS)
    await tftp.exec([
        uboot_msg("help:", bold=True),
        *msgs,
        ], final=True)

async def _probe_flash (tftp, env: dict[str, str]) -> int:
    sz = await uboot_nor_probe(
        tftp,
        max_size=env.get('max', None),
        pre_cmds=[uboot_msg("Probing NOR flash... ", nl=False, bold=True)],
        post_cmds=[uboot_msg('OK')],
    )
    return sz

async def cmd_flash_probe (tftp, ident: str, env: dict[str, str]):
    sz = await _probe_flash (tftp, env)
    await tftp.exec(uboot_msg(f'NOR size={sz//2**10}k'), final=True)

async def cmd_flash_backup (tftp, ident: str, env: dict[str, str]):
    sz = await _probe_flash (tftp, env)
    filename = env.get ('filename', '')
    if not filename:
        filename = f"snapshot-{ident}-{datetime.now():%Y%m%d-%H%M%S}.bin"
    binary = await uboot_nor_download(
        tftp,
        sz,
        pre_cmds=[uboot_msg(f"Copying {sz//2**20}M flash to RAM... ", bold=True, nl=False)],
        post_cmds=[
            uboot_msg("OK"),
            uboot_msg("Downloading backup via TFTP...", bold=True),
        ],
    )
    filename = f'backup/{filename}'
    tftp.write_file (filename, binary)
    msg = uboot_msg (f'  Saved backup as {filename}')
    await tftp.exec([msg], final=True)

async def cmd_flash_restore (tftp, ident: str, env: dict[str, str]):
    err=False
    requires = []
    filename = env.get('filename', None)
    if not filename:
        tftp.exec_queue([uboot_err("filename MUST be specified.")])
        err = True
    else:
        try:
            binary = tftp.read_file(filename)
            sz = await _probe_flash (tftp, env)
            if len(binary) != sz:
                tftp.exec_queue([uboot_err("Filesize not equal to image size.")])
                err = True
        except:
            tftp.exec_queue([uboot_err(f"Failed to read {filename}.")])
            err = True

    if err:
        await cmd_help(tftp, ident, env)
        #await cmd_help(tftp, ident, env, cmd='@flash_restore')
        return
    
    script = [
        uboot_msg (f"Uploading {Path(filename).name}... ", nl=False, bold=True),
        uboot_fetch_static (tftp, filename, offset=1024, requires=requires),
        uboot_msg ("OK"),
        uboot_msg ("Erasing flash... ", nl=False, bold=True),
        uboot_nor_erase (offset=0, size=sz, requires=requires),
        uboot_msg ("OK"),
        uboot_msg ("Writing flash... ", nl=False, bold=True),
        uboot_nor_write (tftp, nor_offset=0, ram_offset=1024, size=sz, requires=requires),
        uboot_msg ("OK"),
    ]
    await tftp.exec (script, requires=requires, final=True)
    
CMDS = {
    '@bootstrap' :
    {
        'handler' : cmd_bootstrap,
        'help' : [
            'Bootstrap framework variables for session calls.',
            '  args:',
            '    ipmode=<static|dhcp> default=dhcp',
            '      static : Do not touch networking for session',
            '      dhcp   : Init networking with dhcp when $netdone != 1'
        ]
    },
    '@help' :
    {
        'handler' : cmd_help,
        'help' : [
            'List of commands available.',
            'args:',
            '  <empty> : Show commands',
            '  vars=X  : Show framework variables',
            'Execute commands with:',
            '  cmd=<cmd>; args=key1=arg1/key2=arg2; run session',
        ]
    },
    '@flash_probe' :
    {
        'handler' : cmd_flash_probe,
        'help' : [
            'Probe flash size',
            'args:',
            '  max=<n>M  ie: max=8M - Limit detected size',
            'Only NOR flash supported currently.',
        ]
    },
    '@flash_backup' :
    {
        'handler' : cmd_flash_backup,
        'help' : [
            'Backup flash via TFTP',
            'args:',
            '  max=<n>M  - Limit backup size'
            '  filename=<filename> default=<datetime>',
            'Only NOR flash supported currently.',
        ]
    },
    '@flash_restore' :
    {
        'handler' : cmd_flash_restore,
        'help' : [
            'Restore flash from binary',
            'args:',
            '  max=<n>M  - Limit detected size to <n>M',
            '  filename=<filename> (required)',
            'Only NOR flash supported currently.',
        ]
    },
}

async def default(tftp, ident: str, cmd: str, env: dict[str, str]):
    if cmd not in CMDS:
        tftp.exec_queue([
            uboot_err(f'Command `{cmd}` not found.')
        ])
    c = CMDS.get (cmd, CMDS['@help'])
    await c['handler'] (tftp, ident, env)
