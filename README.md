[![Co-authored with ChatGPT Codex](https://img.shields.io/badge/co--authored%20with-ChatGPT%20Codex-10a37f)](https://openai.com)

This project is still a work in progress. Expect the API, example scripts, and operational details to change while the design is still settling; it should not be considered stable yet.

TODO:
- [x] openipc: Use u-boot from latest tag if selected tag doesn't contain it on install.
- [ ] openipc: Set mtdparts uboot env variable based on flash size.
- [x] probe available commands in preflight.
  - [x] Verify commands dynamically when running with cache.
- [ ] Add internal handling of `bootstrap` and `bootstrap_onboot` tftp commands.
  - [ ] Install the `netinit` and `bootstrap` commands when found.
  - [ ] `bootstrap_onboot` should inject itself into bootcmd with a failover for normal boot when tftp times out.
  - [ ] `unbootstrap` to revert (as a baked in command, not dependent on tftp server running).
  - [ ] Save passed id to use on future bootstrap calls.
  - [ ] Save list of discovered commands/properties to minimize preflight calls.
  - [ ] Use unique namespace prefix for saved commands.
  - [ ] echo link to terminal with instructions to setup config.toml/scriptfile for specific id.
  - [ ] Hosted locally only via python fastapi.
- [ ] Add script logging per session.
- [ ] Generate FIT image with recorded scripts and orchestrator.
- [x] Add tftp.exec_queue() for non-blocking functions. ie: echo, etc
  - [x] Queue commands until exec happens. Flush commands on completion.
- [x] Fix filename on extracted kernel/rootfs.
- [x] Create scoped u-boot variables to avoid u-boot env contamination and expose tftp.bind() for user scripts.

# uboot-tftp

Session-aware TFTP server for U-Boot style workflows. In addition to standard TFTP get/put operations it can
act as a remote command and control server to implement advanced logic from a user supplied python script. The operations
include python wrappers for calling builtin u-boot cmds, downloading repo assets from github and more. The python logic is
synchronous so different operations can be performed based on the result of previous operations.

There are only two request modes:

- RRQ or WRQ without `id=<ident>`: handled as a normal static TFTP file operation under the configured root directory.
- RRQ starting with `id=<ident>`: handled as part of a session.

## Sample implementation - OpenIPC: https://wiki.openipc.org/

uboot-tftp is a python scripted dynamic tftp server.

It only requires the following dependencies:

- uboot with hush parser enabled (CONFIG_HUSH_PARSER=y)
- Networking with tftp commands: tftp/tftpboot, source, tftpput[optional], dhcp[optional]
- baseaddr/loadaddr environment variable pointing to the RAM base

As a proof of concept, an implementation for installation of the openipc project has been implemented.

See [openipc.py](src/uboot_tftp/openipc.py) for the reference implementation.

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
Executing preflight... OK
Fetching current uboot environment... OK
Probing NOR flash... OK
Downloading github/OpenIPC/firmware/releases/tags/latest.json: 750.6 kB
Downloading OpenIPC/firmware/releases/tags/latest/gk7205v300/u-boot-gk7205v300-universal.bin: 250.2 kB
Downloading OpenIPC/firmware/releases/tags/latest/gk7205v300/openipc.gk7205v300-nor-lite.tgz: 6867.5 kB
Copying NOR flash to RAM... OK
Partition update plan:
uboot    0x00000000 size=0x00040000 src=u-boot-gk7205v300-universal.bin  flash=0xb7094978 payload=0x81686d05 update
env      0x00040000 size=0x00010000 src=cam-final-env.bin                flash=0xdeab7e4e payload=0x22f7eb0a update
kernel   0x00050000 size=0x00300000 src=uImage.gk7205v300                flash=0xaeecf75a payload=0xfcdeff25 update
rootfs   0x00350000 size=0x00a00000 src=rootfs.squashfs.gk7205v300       flash=0xdb3bf1c2 payload=0x287307ad update
Uploading u-boot-gk7205v300-universal.bin... OK
Erasing uboot... OK
Writing uboot... OK
Uploading cam-final-env.bin... OK
Erasing env... OK
Writing env... OK
Uploading uImage.gk7205v300... OK
Erasing kernel... OK
Writing kernel... OK
Uploading rootfs.squashfs.gk7205v300... OK
Erasing rootfs... OK
Writing rootfs... OK

Install finished for cam-final
Updated partitions: uboot, env, kernel, rootfs

Rebooting in 10 seconds
Enter Ctrl+C to cancel...
[##        ]
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

Minimal Example [`config.toml`](src/uboot_tftp/openipc.toml):

Here's a more advanced config.toml showing inheritance of env variables and target specific entry points.

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
# These are global arbitrary user defined and will be passed to the env dict
# to be used in the user script
nfsserver = '10.0.70.220'
rootfs = '/mnt/STORAGE/config/camera/boot/rootfs'
kernel = 'uImage.generic'

# Only triggered if id=cam123 in initial RRQ filename.
[cam123]
entry_func = "cam123_entry"
# Target specific entries will override global env above
rootfs = '/mnt/STORAGE/config/camera/boot/rootfs.${hostname}'
kernel = 'uImage.${soc}'

# Triggered for all cases when id= doesn't match any other sections.
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
