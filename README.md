# uboot-tftp

Minimal session-aware TFTP server for OpenIPC and U-Boot style workflows.

There are only two request modes:

- RRQ or WRQ without `id=<ident>`: handled as a normal static TFTP file operation under the configured root directory.
- RRQ or WRQ starting with `id=<ident>`: handled as part of a session.

## Session Model

A session starts when the server receives an RRQ like:

```text
id=cam123/bootstrap
```

Before the user handler runs, `uboot-tftp` performs an internal preflight to verify that the target U-Boot has a hush-compatible shell. Session handlers only start after that check succeeds.

The server creates a new session for `cam123` and calls the matching user handler from `script.py`. If no matching section exists in `config.toml`, the `[default]` handler is used.

Session handlers are `async def` functions. They use these helpers:

- `await tftp.exec(script, final=False)`
- `await tftp.exec_recv(script, size)`
- `await tftp.fetch_env()`
- `tftp.write_file(path, body)`

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

Example [`config.toml`](/home/elliot/work/openipc/openipc-tftp/config.toml:1):

```toml
[server]
scriptfile = "script.py"
rootdir = "/absolute/path/to/files"
address = "::"
port = 6969
timeout = 5
retries = 3
log_level = "INFO"

[env]
rambase = "loadaddr"
cmdtftp = "tftpboot"
cmdtftpput = "tftpput"

[cam123]
entry_func = "camera_bootstrap"

[default]
entry_func = "default"
```

## Script API

Example [`script.py`](/home/elliot/work/openipc/openipc-tftp/script.py:1):

```python
from uboot_tftp.scripted import ReceiveFailedError


async def default(tftp, ident, cmd, env):
    await tftp.exec(
        [
            f"echo default session for {ident}",
            f"echo requested cmd: {cmd}",
            f"echo env hostname: {env.get('hostname', '<unset>')}",
        ],
        final=True,
    )


async def camera_bootstrap(tftp, ident, cmd, env):
    if cmd == "bootstrap":
        await tftp.exec(
            [
                f"echo preparing {ident}",
                f"echo using {tftp.rambase}",
            ]
        )

        try:
            env = await tftp.fetch_env()
        except ReceiveFailedError:
            await tftp.exec(["echo upload failed"], final=True)
            return

        await tftp.exec([f"echo bootstrap complete {env.get('ethaddr', '<unknown>')}"], final=True)
        return

    await tftp.exec([f"echo unknown cmd {cmd}"], final=True)
```

Example uploading from a RAM offset:

```python
async def dump_region(tftp, ident, cmd, env):
    if cmd != "dump":
        await tftp.exec(["echo unknown cmd"], final=True)
        return

    await tftp.exec_recv(
        ["echo uploading memory region"],
        0x1000,
        offset=0x400,
    )
    await tftp.exec(["echo upload complete"], final=True)
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
