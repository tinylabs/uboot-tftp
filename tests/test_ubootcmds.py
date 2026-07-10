from uboot_tftp.ubootcmds import (
    build_probe_batch,
    command_registry,
    framework_emitted_commands,
)


def test_every_framework_emitted_command_is_represented_in_registry():
    registry = command_registry()
    missing = [command for command in framework_emitted_commands() if command not in registry]
    assert missing == []


def test_every_probe_entry_has_probe_script_and_return_key():
    registry = command_registry()
    probe_commands = [name for name, spec in registry.items() if spec.policy == "probe"]
    for command in probe_commands:
        assert registry[command].probe_lines

    _, keys, key_map = build_probe_batch(probe_commands, {"rambase": "loadaddr"})
    assert len(keys) == len(probe_commands)
    assert set(key_map) == set(probe_commands)
    assert keys == [f"_{index}" for index in range(len(keys))]


def test_probe_batch_uses_configured_rambase_variable():
    lines, _, _ = build_probe_batch(["sf read", "env export"], {"rambase": "baseaddr"})
    assert "sf read ${baseaddr} 0x0 0x0" in lines
    assert "env export -t ${baseaddr}" in lines


def test_setexpr_probe_captures_status_from_setexpr_itself():
    lines, keys, _ = build_probe_batch(["setexpr"], {"rambase": "loadaddr"})
    assert lines == [
        "setexpr __uboot_tftp_probe 1 + 1",
        f"setenv {keys[0]} $?",
    ]


def test_every_config_alias_has_config_key():
    registry = command_registry()
    alias_specs = [spec for spec in registry.values() if spec.policy == "config_alias"]
    assert alias_specs
    assert all(spec.config_key for spec in alias_specs)


def test_source_is_classified_as_session_assumed():
    spec = command_registry()["source"]
    assert spec.policy == "assumed"
    assert spec.assumption == "session_established"


def test_env_export_is_classified_as_probe_required():
    spec = command_registry()["env export"]
    assert spec.policy == "probe"
