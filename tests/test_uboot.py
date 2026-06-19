import struct

from openipc_tftp.mkimage import extract_script_payload
from openipc_tftp.providers import ContentRequest
from openipc_tftp.uboot import UBootScriptProvider, UBootScriptRenderer


def script_from_result(result):
    return extract_script_payload(result.body).decode("utf-8")


def test_uboot_script_provider_returns_compiled_script_image():
    provider = UBootScriptProvider(
        renderer=UBootScriptRenderer(commands=('echo "test"',), continue_loop=False)
    )
    result = provider.fetch(
        ContentRequest(
            filename="ethaddr=aa:bb:cc:dd:ee:ff/",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )

    assert result.size == len(result.body)
    assert isinstance(result.body, bytes)
    assert extract_script_payload(result.body).endswith(b'echo "test"\n')


def test_uboot_script_provider_tracks_env_by_ethaddr():
    provider = UBootScriptProvider(renderer=UBootScriptRenderer(continue_loop=False))

    provider.fetch(
        ContentRequest(
            filename="ethaddr=aa:bb:cc:dd:ee:ff/env/ipaddr=192.168.1.50",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )

    session = provider.sessions.get_or_create("aa:bb:cc:dd:ee:ff")
    assert session.env == {"ipaddr": "192.168.1.50"}
    assert session.sequence == 1


def test_compiled_provider_response_uses_uboot_script_type():
    provider = UBootScriptProvider(renderer=UBootScriptRenderer(continue_loop=False))
    result = provider.fetch(
        ContentRequest(
            filename="ethaddr=aa:bb:cc:dd:ee:ff/",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )

    fields = struct.unpack(">7I4B32s", result.body[:64])
    assert fields[9] == 6


def test_get_uboot_var_queues_variable_read_script():
    provider = UBootScriptProvider(renderer=UBootScriptRenderer(commands=()))
    provider.get_uboot_var("ipaddr")

    result = provider.fetch(
        ContentRequest(
            filename="ethaddr=aa:bb:cc:dd:ee:ff/",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )

    script = script_from_result(result)
    assert 'echo "getting ipaddr"' in script
    assert 'if tftpboot ${baseaddr} "${serverip}:ethaddr=${ethaddr}/var/ipaddr=${ipaddr}"' in script
    assert "then source ${baseaddr};" in script
    assert 'else echo "openipc-tftp: stopping because tftpboot failed"; fi' in script


def test_set_uboot_var_queues_variable_write_script_with_saveenv():
    provider = UBootScriptProvider(renderer=UBootScriptRenderer(commands=()))
    provider.set_uboot_var("bootdelay", "3", saveenv=True)

    result = provider.fetch(
        ContentRequest(
            filename="ethaddr=aa:bb:cc:dd:ee:ff/",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )

    script = script_from_result(result)
    assert "setenv bootdelay 3" in script
    assert "saveenv" in script
    assert 'if tftpboot ${baseaddr} "${serverip}:ethaddr=${ethaddr}/set/bootdelay=ok"' in script


def test_targeted_action_waits_for_matching_ethaddr():
    provider = UBootScriptProvider(renderer=UBootScriptRenderer(commands=()))
    provider.get_uboot_var("ipaddr", ethaddr="aa:bb:cc:dd:ee:ff")

    other_result = provider.fetch(
        ContentRequest(
            filename="ethaddr=11:22:33:44:55:66/",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )
    target_result = provider.fetch(
        ContentRequest(
            filename="ethaddr=aa:bb:cc:dd:ee:ff/",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )

    assert "/var/ipaddr=" not in script_from_result(other_result)
    assert "/var/ipaddr=" in script_from_result(target_result)


def test_run_uboot_var_renders_run_and_completion_callback():
    provider = UBootScriptProvider(renderer=UBootScriptRenderer(commands=()))
    provider.run_uboot_var("bootcmd")

    result = provider.fetch(
        ContentRequest(
            filename="ethaddr=aa:bb:cc:dd:ee:ff/",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )

    script = script_from_result(result)
    assert "run bootcmd" in script
    assert 'if tftpboot ${baseaddr} "${serverip}:ethaddr=${ethaddr}/run/bootcmd=ok"' in script


def test_run_uboot_commands_renders_inline_batch():
    provider = UBootScriptProvider(renderer=UBootScriptRenderer(commands=()))
    provider.run_uboot_commands(["echo one", "echo two"], name="smoke")

    result = provider.fetch(
        ContentRequest(
            filename="ethaddr=aa:bb:cc:dd:ee:ff/",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )

    script = script_from_result(result)
    assert "echo one" in script
    assert "echo two" in script
    assert 'if tftpboot ${baseaddr} "${serverip}:ethaddr=${ethaddr}/run/smoke=ok"' in script


def test_printenv_renders_serial_print_and_completion_callback():
    provider = UBootScriptProvider(renderer=UBootScriptRenderer(commands=()))
    provider.printenv(["ipaddr"])

    result = provider.fetch(
        ContentRequest(
            filename="ethaddr=aa:bb:cc:dd:ee:ff/",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )

    script = script_from_result(result)
    assert "printenv ipaddr" in script
    assert 'if tftpboot ${baseaddr} "${serverip}:ethaddr=${ethaddr}/printenv/printenv=ok"' in script


def test_report_renders_generic_report_callback():
    provider = UBootScriptProvider(renderer=UBootScriptRenderer(commands=()))
    provider.report("filesize", "${filesize}")

    result = provider.fetch(
        ContentRequest(
            filename="ethaddr=aa:bb:cc:dd:ee:ff/",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )

    script = script_from_result(result)
    assert 'if tftpboot ${baseaddr} "${serverip}:ethaddr=${ethaddr}/report/filesize=${filesize}"' in script


def test_probe_queues_common_variable_reads():
    provider = UBootScriptProvider(renderer=UBootScriptRenderer(commands=()))
    provider.probe()

    first = provider.fetch(
        ContentRequest(
            filename="ethaddr=aa:bb:cc:dd:ee:ff/",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )

    assert "/var/ipaddr=" in script_from_result(first)


def test_export_env_renders_env_export_and_tftpput_upload():
    provider = UBootScriptProvider(renderer=UBootScriptRenderer(commands=()))
    provider.export_env()

    result = provider.fetch(
        ContentRequest(
            filename="ethaddr=aa:bb:cc:dd:ee:ff/",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )

    script = script_from_result(result)
    assert "env export -t ${loadaddr}" in script
    assert (
        'if tftpput ${loadaddr} ${filesize} "${serverip}:'
        'ethaddr=${ethaddr}/upload/env.txt";'
    ) in script
    assert 'if tftpboot ${baseaddr} "${serverip}:ethaddr=${ethaddr}/export-env/export-env=ok"' in script


def test_export_env_allows_custom_upload_path_and_address():
    provider = UBootScriptProvider(renderer=UBootScriptRenderer(commands=()))
    provider.export_env(path="upload/full-env.txt", address="0x43000000")

    result = provider.fetch(
        ContentRequest(
            filename="ethaddr=aa:bb:cc:dd:ee:ff/",
            peer=("127.0.0.1", 12345),
            server_addr=("127.0.0.1", 6969),
            options={"mode": "octet"},
        )
    )

    script = script_from_result(result)
    assert "env export -t 0x43000000" in script
    assert (
        'if tftpput 0x43000000 ${filesize} "${serverip}:'
        'ethaddr=${ethaddr}/upload/full-env.txt";'
    ) in script
