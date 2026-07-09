[![Co-authored with ChatGPT Codex](https://img.shields.io/badge/co--authored%20with-ChatGPT%20Codex-10a37f)](https://openai.com)

This project is still a work in progress. Expect the API, example scripts, and operational details to change while the design is still settling; it should not be considered stable yet.

TODO:
- probe available commands in preflight.
  - Check $? when running commands without args
- Add internal handling of `bootstrap` and `bootstrap_onboot` tftp commands.
  - Install the `netinit` and `bootstrap` commands when found.
  - `bootstrap_onboot` should inject itself into bootcmd with a failover for normal boot when tftp times out.
  - `unbootstrap` to revert (as a baked in command, not dependent on tftp server running).
  - Save passed id to use on future bootstrap calls.
  - Save list of discovered commands/properties to minimize preflight calls.
  - Use unique namespace prefix for saved commands.
  - echo link to terminal with instructions to setup config.toml/scriptfile for specific id.
  - Hosted locally only via python fastapi.
- Add script logging per session.
- Generate FIT image with recorded scripts and orchestrator.

# uboot-tftp

Minimal session-aware TFTP server for OpenIPC and U-Boot style workflows.

There are only two request modes:

- RRQ or WRQ without `id=<ident>`: handled as a normal static TFTP file operation under the configured root directory.
- RRQ starting with `id=<ident>`: handled as part of a session.

## Sample implementation - OpenIPC: https://wiki.openipc.org/

uboot-tftp is a generic python scripted dynamic tftp server.

It only requires the following dependencies:

- uboot with hush parser enabled (CONFIG_HUSH_PARSER=y)
- Networking with tftp commands: tftp/tftpboot, source, tftpput[optional], dhcp[optional]
- baseaddr/loadaddr environment variable pointing to the RAM base

As a proof of concept, an implementation for installation of the openipc project has been implemented.

See [openipc.py](scripts/openipc.py) for the reference implementation.

## Installation

Install from GitHub with pip:

```bash
pip install git+https://github.com/tinylabs/uboot-tftp.git
```

This installs the core CLI tools:

- `uboot-tftp`
- `uboot-tftp-check`
- `uboot-tftp-client`
- `uboot-tftp-env`

It also installs a packaged OpenIPC example:

- `openipc-tftp`
- packaged `openipc.toml`
- packaged `openipc.py`

`openipc-tftp` starts the server with the packaged OpenIPC config and script. By default it uses `rootdir = "/tmp/openipc-tftp"`. Override that at runtime with `--rootdir`.

On Linux, binding to UDP port `69` normally requires root. If you want to run the server as a normal user without `sudo`, you can temporarily lower the unprivileged port floor:

```bash
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=69
```

That change is runtime-only and reverts on reboot. To undo it immediately:

```bash
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=1024
```

If you would rather avoid changing the sysctl, run the server with `sudo` or configure a higher TFTP port instead of `69`.

Below is the terminal output showing openipc install using the server from a networked camera.

```
OpenIPC # printenv bootstrap
bootstrap=run netinit; if tftpboot ${baseaddr} ${serverip}:id=${hostname}/${cmd}/${args}; then source ${baseaddr}; else echo "TFTP request failed: is TFTP server running?"; fi
OpenIPC # printenv netinit  
netinit=if test "${ip}" = "static" || test -n "$netdone" && test "$netdone" -eq 1; then echo "Networking OK"; else setenv autoload no; dhcp; netdone=1; fi
OpenIPC #
OpenIPC # hostname=cam-final; cmd=install; args=vendor=goke/soc=gk7205v300; run bootstrap
Checking hush shell... 
Fetching current uboot environment... OK
Probing NOR flash... 0x1000000
Backing up NOR flash.
Copying NOR to RAM... OK
Downloading backup via TFTP...
  Saved backup as backup/install-backup-cam-final-gk7205v300-16mb-20260630-094514.bin
Using cached download: install/openipc-gk7205v300-lite-16mb.bin.
Extracting uboot env from image... OK
Patched env variables:
  Reusing ethaddr from old env
  ethaddr    = 02:2a:0e:f2:cb:41
  hostname   = cam-final
  bootp_vci  = uboot.cam-final
  serverip   = 10.0.1.20
  ipaddr     = 10.0.50.179
  netmask    = 255.255.255.0
  gatewayip  = 10.0.50.1
  fw         = lite
  ip         = dhcp
Uploading cam-final-openipc-gk7205v300-lite-16mb.bin... OK
Erasing flash... OK
Writing flash... OK

Install finished for cam-final
------------------------------
Flash backup: /home/elliot/work/openipc/uboot-tftp/files/install-backup-cam-final-gk7205v300-16mb-20260630-094514.bin
Web UI: http://10.0.50.179/
SSH: ssh root@10.0.50.179 (password: 12345)
Support OpenIPC: https://opencollective.com/openipc/contribute

Rebooting in 10 seconds
Enter Ctrl+C to cancel...
Rebooting...
```

## Session Model

A session starts when the server receives an RRQ like:

```text
id=cam123/<cmd>/[key1=arg1/key2=arg2/...]
```

Before the user handler runs, `uboot-tftp` performs an internal preflight to verify that the target U-Boot has a hush-compatible shell. Session handlers only start after that check succeeds.

The server creates a new session for `cam123` and calls the matching user handler from toml `scriptfile`. If no matching section exists in `config.toml`, the `[default]` handler is used.

Session handlers are `async def` functions. They use these helpers:

- `await tftp.exec(script, final=False)`
- `await tftp.exec_recv(script, size)`
- `await tftp.fetch_env()`
- `tftp.write_file(path, body)`
- ...

`exec(...)` sends a script to the client. If `final=False`, the server appends an internal continuation `tftpboot` so the session can continue on the next RRQ.

`exec(..., final=True)` sends the script without appending continuation. That ends the session.

`exec_recv(...)` sends a script that:

1. runs your commands
2. performs an internal `tftpput`
3. performs an internal continuation `tftpboot`

When the client returns on the continuation RRQ, `exec_recv(...)` resumes and returns the uploaded bytes to the handler.

If the upload fails and the client returns on the failure continuation path, `exec_recv(...)` raises `ReceiveFailedError`.

`exec_recv(...)` also accepts `offset=...` to upload from `tftp.rambase + offset` instead of the base address itself.

## Config

Example [`config.toml`](config/openipc.toml):

```toml
[server]
scriptfile = "../scripts/openipc.py"
rootdir = "/mnt/tftp"
address = "0.0.0.0"
port = 69
timeout = 5
retries = 3
log_level = "info"

[env]
# These 3 variables are required for server script generation
rambase = "baseaddr"
cmdtftp = "tftpboot"
cmdtftpput = "tftpput"
# These are arbitrary user defined and will be passed to the env dict
# to be used in the user script
nfsserver = '10.0.70.220'
rootfs = '/mnt/STORAGE/config/camera/boot/rootfs'
kernel = 'uImage.generic'

[cam123]
entry_func = "cam123_entry"
# Target specific entries will override global env above
rootfs = '/mnt/STORAGE/config/camera/boot/rootfs.${hostname}'
kernel = 'uImage.${soc}'

[default]
# This will get called if a matching 'id=' entry isn't found.
entry_func = "default"
```

When installed from pip, the packaged OpenIPC example uses an installed copy of `openipc.py` and a packaged `openipc.toml`, so you do not need a local checkout just to run `openipc-tftp`.

## Script API

```python
from uboot_tftp.ubootscript import *
from uboot_tftp.ubootops import *
from uboot_tftp.ubootterm import *
from uboot_tftp.ubootenv import *

from uboot_tftp.scripted import ReceiveFailedError

async def default(tftp, ident, cmd, tftp_env):
    # Fetch current environment                         
    cenv = await tftp.fetch_env(
        upload_script=[
            # Colorized ANSI terminal support with uboot_msg helper
            uboot_msg ("Fetching current uboot environment... ", bold=True),
        ]
    )

    # Run commands and dynamically fetch set variables
    mac = '02:11:22:33:44:55'
    await tftp.exec ([
        uboot_msg(f"Getting new IP with ethaddr={mac}..."),
        f'setenv ethaddr {mac}',
        'setenv autoload no',
        'dhcp',
        uboot_msg("Success: ip=${ipaddr} mask=${netmask} gateway=${gatewayip}")
    # keys(optional) specifies which tftp_env dict keys to update
    ], keys=['ipaddr', 'netmask', 'gatewayip'])

    # These dict items were dynamically added on completion of the above command
    print (f'DHCP: {tftp_env["ipaddr"]}:{tftp_env["netmask"]} GW:{tftp_env["gatewayip"]}')

# Entry points must match this signature
async def cam123_entry(tftp, ident, cmd, tftp_env):
    match cmd:
        # Just boot normally
        case 'boot':
            # No final=True flag needed, this forces end of session
            await uboot_boot (tftp)
        # Boot with NFS root
        case 'bootnfs':
            bootargs = ' '.join ([f'mem=${{totalmem}}',
                                  'console=ttyAMA0,115200',
                                  'panic=20',
                                  'root=/dev/nfs',
                                  'ip=dhcp',
                                  f'nfsroot={tftp_env["nfsserver"]}:{tftp_env["rootfs"]},v3,nolock',
                                  'rw'])
            await tftp.exec([
                f'setenv bootargs {bootargs}',
                uboot_msg ('Booting from NFS...'),
                uboot_msg (f'bootargs=${{bootargs}}'),
                f'tftpboot {tftp.rambase} {tftp.server_ip}:${{hostname}}/{tftp_env["kernel"]}; bootm {tftp.rambase}',
            ], final=True)
        case '_': # Command doesn't match
             await tftp.exec([uboot_err(f"Command: {cmd} invalid")], final=True)

```

## Static Files

Files under `rootdir` are served directly for bare RRQ requests and written directly for bare WRQ requests.

`scriptfile` may be relative to the directory containing `config.toml`, or absolute. `rootdir` must be an absolute path.

`log_level` uses Python logging levels. At `INFO`, `uboot-tftp` logs one-line RRQ/WRQ open and completion summaries, while `tftpy`'s lower-level packet chatter is suppressed. Set `log_level = "DEBUG"` when you want the underlying `tftpy` transfer details too.

`--rootdir /absolute/path` overrides `[server].rootdir` from the TOML file.

Example:

```text
RRQ uImage
WRQ backup.bin
```

These map to files under `/absolute/path/to/files/` when `rootdir = "/absolute/path/to/files"`.

## Running

```bash
uboot-tftp --config config.toml
uboot-tftp --config config.toml --rootdir /absolute/path/to/files
```

To run the packaged OpenIPC example installed from pip:

```bash
openipc-tftp
openipc-tftp --rootdir /absolute/path/to/files
```

## Extracting U-Boot Env

You can inspect the effective U-Boot env from a full flash image directly:

```bash
uboot-tftp-env firmware.bin
```

When partition boundaries are known, pass them explicitly:

```bash
uboot-tftp-env firmware.bin --boot-size 0x40000 --env-offset 0x40000 --env-size 0x10000
```

For machine-readable output:

```bash
uboot-tftp-env firmware.bin --format json
```

You can also patch the env partition in a full flash image:

```bash
uboot-tftp-env firmware.bin \
  --output firmware.patched.bin \
  --set bootcmd='run custom' \
  --set serverip=10.0.0.1
```

Or load the replacement env from a JSON object:

```bash
uboot-tftp-env firmware.bin \
  --output firmware.patched.bin \
  --env-json env.json
```

## Simulated Client

You can exercise the session flow without hardware:

```bash
uboot-tftp-client 127.0.0.1 --id cam123 --path /bootstrap
```

The simulated client:

- downloads script images with RRQ
- prints the script contents
- echoes non-transfer commands to the terminal
- follows embedded continuation `tftpboot` requests
- uploads dummy binary data for embedded `tftpput` requests

It also keeps the old image-extraction mode:

```bash
uboot-tftp-client boot.uimg
```
