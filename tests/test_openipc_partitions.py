import importlib.util
from pathlib import Path


def load_openipc_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "openipc.py"
    spec = importlib.util.spec_from_file_location("openipc_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openipc_partition_table_prefers_size_specific_nor_layout():
    module = load_openipc_module()
    env = {
        "mtdparts": "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
        "mtdpartsnor16m": (
            "setenv mtdparts "
            "sfc:256k(boot),64k(env),3072k(kernel),10240k(rootfs),-(rootfs_data)"
        ),
    }

    table = module.openipc_partition_table(env, flash_type="nor", flash_size=16 * 2**20)

    assert table.device == "sfc"
    assert table.range("kernel") == (0x50000, 0x300000)
    assert table.range("rootfs") == (0x350000, 0xA00000)


def test_openipc_partition_table_falls_back_to_default_mtdparts():
    module = load_openipc_module()
    env = {
        "mtdparts": "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
    }

    table = module.openipc_partition_table(env, flash_size=8 * 2**20)

    assert table.range("boot") == (0x00000, 0x40000)
    assert table.range("env") == (0x40000, 0x10000)
    assert table.range("rootfs_data") == (0x750000, 0x800000 - 0x750000)
