#!/usr/bin/env python3
"""Minimal example handler module for openipc-tftp."""

from __future__ import annotations

from openipc_tftp.scripted import ReceiveFailedError


async def default(tftp, ident: str, cmd: str, env: dict[str, str]):
    print (env)
    await tftp.exec(
        [
            f"echo using {tftp.rambase}",
            f"echo default session for {ident}",
            f"echo requested cmd: {cmd}",
            f"echo env hostname: {env.get('hostname', '<unset>')}",
        ],
        final=True,
    )


async def camera_bootstrap(tftp, ident: str, cmd: str, env: dict[str, str]):
    print (env)
    if cmd != "bootstrap":
        await tftp.exec([f"echo unknown cmd {cmd}"], final=True)
        return

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
