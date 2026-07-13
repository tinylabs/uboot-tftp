"""Built-in special-command script overrides."""

from __future__ import annotations

import re
from .ubootterm import *

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
            'fi']),
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
    msgs = [
        uboot_msg ('Bootstrap complete.'),
        uboot_msg (f'Installed {len(cmds)} env variables: {list(var_dict.keys())}', bold=True),
        uboot_msg("Run `saveenv` to persist across reboot", color='yellow'),
        uboot_msg('Run `cmd=@help; run session` to view commands.', color='yellow'),
        uboot_msg('Run `cmd=@help; args=vars=1; run session` to view variables.', color='yellow'),
    ]
    await tftp.exec(cmds + msgs, final=True)

async def cmd_help (tftp, ident: str, env: dict[str, str]):
    if 'vars' in env:
        var_dict = _session_vars(tftp, ident, env)
        msgs = _help_msgs (var_dict, expand=True)
    else:
        msgs = _help_msgs (CMDS)
    await tftp.exec([
        uboot_msg("help:", bold=True),
        *msgs,
        ], final=True)

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
}

async def default(tftp, ident: str, cmd: str, env: dict[str, str]):
    if cmd not in CMDS:
        tftp.exec_queue([
            uboot_err(f'Command `{cmd}` not found.')
        ])
    c = CMDS.get (cmd, CMDS['@help'])
    await c['handler'] (tftp, ident, env)
