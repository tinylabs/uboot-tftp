# openipc-tftp

`openipc-tftp` is a small Python package scaffold for building dynamic TFTP
servers on top of [`tftpy`](https://pypi.org/project/tftpy/).

The package keeps the dynamic content decision behind a provider interface. The
RRQ filename, peer address, server address, and negotiated options are passed to
that provider, and the provider returns bytes or a binary stream that `tftpy`
can send back to the client.

## Status

This is a scaffold. It includes packaging metadata, a `tftpy` adapter layer,
a minimal CLI, dynamic RRQ responses, and WRQ/tftpput upload capture.

## Install for Development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install -e .
pytest
```

For runtime-only installs from a checkout, use:

```bash
python -m pip install -r requirements.txt
python -m pip install -e .
```

## U-Boot Protocol

The primary client key is an identifier containing letters, digits, `-`, and `_`
encoded into the RRQ
filename:

```text
id=cam123/
id=cam123/env/ipaddr=192.168.1.50/serial=abc123
```

The first segment must be `id=<identifier>`. Remaining path segments are protocol
data. Segments in `key=value` form are parsed and attached to the in-memory
client session. The built-in session store is process-local and keyed by
client identifier.

The intended U-Boot bootstrap is:

```bash
setenv autoload no; dhcp; tftpboot ${baseaddr} "${serverip}:id=cam123/"; source ${baseaddr}
```

Responses are generated as U-Boot legacy script images in pure Python, matching
the shape produced by `mkimage -T script`. No system `mkimage` binary is needed.

## Provider Shape

```python
from openipc_tftp import ContentRequest, ContentResult


def fetch_content(request: ContentRequest) -> ContentResult:
    if request.filename == "hello.txt":
        return ContentResult.from_bytes(b"hello\n")
    raise FileNotFoundError(request.filename)
```

## Server Shape

```python
from openipc_tftp import CallableContentProvider, DynamicContentServer


server = DynamicContentServer(
    address="::",
    port=6969,
    retries=3,
    timeout=5,
    provider=CallableContentProvider(fetch_content),
)
server.run()
```

## Daemon Config

The CLI runs as a config-driven daemon and takes one argument: a TOML file.

```bash
openipc-tftp config.toml
```

The TOML file has `[server]`, `[env]`, one section per known client ID, and a
`[default]` route:

```toml
[server]
scriptfile = "script.py"
upload = "/tmp/openipc-tftp-upload"
address = "::"
port = 6969

[env]
cmdtftp = "tftpboot"
ramvar = "baseaddr"

[cam123]
script = "boot_nfs"

[default]
script = "default"
```

`[server]` controls daemon settings. Supported keys include `scriptfile`,
`upload` or `upload_dir`, `address`, `port`, `retries`, `timeout`, and
`log_level`. `[env]` provides base environment values for scripts. The daemon
uses `[env].cmdtftp` when it generates follow-up download commands and expands
`[env].ramvar` as a U-Boot variable reference, for example `ramvar = "baseaddr"`
becomes `${baseaddr}`.

Each route section names a Python function from `scriptfile`. The function is
called as:

```python
def default(uboot, ident, path):
    env = uboot.get_env()
    uboot.send_noreply(f"echo booting {ident} from {path}")
```

The first argument exposes:

- `get_env()`: exports the target U-Boot environment with `tftpput`, receives
  it, and returns `[env]` merged with target values. Target values win.
- `send(script)`: sends a script and appends a guarded follow-up RRQ for the
  same `id=` and path.
- `send_noreply(script)`: sends a script without appending another RRQ.

Example U-Boot upload path used by `get_env()`:

```bash
tftpput ${baseaddr} ${filesize} "${serverip}:id=cam123/upload/env.txt"
```

That example writes under the configured upload directory as
`<identifier>/upload/env.txt`. The `id=` prefix is removed from the directory
name.

For local testing with a desktop TFTP client:

```bash
tftp 127.0.0.1 6969 -c get 'id=cam123/' /tmp/boot.scr.uimg
```

U-Boot can request a literal `id=<identifier>/` filename, and follow-up scripts
continue using that original identifier.

For multi-round-trip testing without a target, run the daemon in one shell and
the helper client in another. The helper uses the system `tftp` executable for
RRQ and WRQ transfers, prints each returned script, uploads a mock exported
environment when `get_env()` asks for one, and follows generated continuation
RRQs:

```bash
openipc-tftp config.toml
openipc-tftp-client 127.0.0.1 --port 6969 --id camfront --path /bootstrap \
  --target-env hostname=camfront --target-env bootcmd='run boot_normal'
```

Use `--target-env-file env.txt` for larger mock environments and `--keep-dir
./client-runs` to retain downloaded script images and generated upload files.

To inspect a returned script image during testing:

```bash
python - <<'PY'
from pathlib import Path
from openipc_tftp import extract_script_payload

print(extract_script_payload(Path("/tmp/boot.scr.uimg").read_bytes()).decode())
PY
```

The image uses the same payload layout as `mkimage -T script`: a 64-byte legacy
image header, an 8-byte script component table, then the script text.

Use a high port during development unless the process has permission to bind to
UDP port 69.

## Publishing Notes

Before publishing, choose and add a license file, confirm the package name on
the target index, and replace the placeholder CLI provider with a production
provider or plugin-loading mechanism.
