# openipc-tftp

Minimal session-aware TFTP server for OpenIPC and U-Boot style workflows.

There are only two request modes:

- RRQ or WRQ without `id=<ident>`: handled as a normal static TFTP file operation under the configured root directory.
- RRQ or WRQ starting with `id=<ident>`: handled as part of a session.

## Session Model

A session starts when the server receives an RRQ like:

```text
id=cam123/bootstrap
```

The server creates a new session for `cam123` and calls the matching user handler from `script.py`. If no matching section exists in `config.toml`, the `[default]` handler is used.

Session handlers are `async def` functions. They use these helpers:

- `await tftp.exec(script, final=False)`
- `await tftp.exec_recv(script, size)`
- `tftp.write_file(path, body)`

`exec(...)` sends a script to the client. If `final=False`, the server appends an internal continuation `tftpboot` so the session can continue on the next RRQ.

`exec(..., final=True)` sends the script without appending continuation. That ends the session.

`exec_recv(...)` sends a script that:

1. runs your commands
2. performs an internal `tftpput`
3. performs an internal continuation `tftpboot`

When the client returns on the continuation RRQ, `exec_recv(...)` resumes and returns the uploaded bytes to the handler.

If the upload fails and the client returns on the failure continuation path, `exec_recv(...)` raises `ReceiveFailedError`.

## Config

Example [`config.toml`](/home/elliot/work/openipc/openipc-tftp/config.toml:1):

```toml
[server]
scriptfile = "script.py"
root = "files"
address = "::"
port = 6969
timeout = 5
retries = 3

[env]
rambase = "loadaddr"
cmdtftp = "tftpboot"
cmdtftpput = "tftpput"

[cam123]
script = "camera_bootstrap"

[default]
script = "default"
```

## Script API

Example [`script.py`](/home/elliot/work/openipc/openipc-tftp/script.py:1):

```python
from openipc_tftp.scripted import ReceiveFailedError


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
            data = await tftp.exec_recv(
                [
                    "echo uploading environment snapshot",
                    f"env export -t {tftp.rambase}",
                ],
                4096,
            )
        except ReceiveFailedError:
            await tftp.exec(["echo upload failed"], final=True)
            return

        tftp.write_file(f"uploads/{ident}-env.txt", data)
        await tftp.exec(["echo bootstrap complete"], final=True)
        return

    await tftp.exec([f"echo unknown cmd {cmd}"], final=True)
```

## Static Files

Files under `root` are served directly for bare RRQ requests and written directly for bare WRQ requests.

Example:

```text
RRQ uImage
WRQ backup.bin
```

These map to files under `files/` when `root = "files"`.

## Running

```bash
openipc-tftp config.toml
```

## Simulated Client

You can exercise the session flow without hardware:

```bash
openipc-tftp-client 127.0.0.1 --id cam123 --path /bootstrap
```

The simulated client:

- downloads script images with RRQ
- prints the script contents
- echoes non-transfer commands to the terminal
- follows embedded continuation `tftpboot` requests
- uploads dummy binary data for embedded `tftpput` requests

It also keeps the old image-extraction mode:

```bash
openipc-tftp-client boot.uimg
```
