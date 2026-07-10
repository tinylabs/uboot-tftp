"""Minimal session-aware TFTP helpers for uboot flows."""

from .config import DaemonConfig, ScriptRoute, load_daemon_config
from .download_jobs import DownloadArtifact, DownloadJobStore, DownloadRequest
from .flashplan import (
    PartitionDigest,
    PartitionPayload,
    PartitionUpdate,
    PartitionUpdatePlan,
    build_partition_update_plan,
    collect_partition_digests,
    partition_payload_crc32,
)
from .github_assets import GithubAsset, GithubJsonManifest
from .mkimage import LegacyScriptImageCompiler, extract_script_payload
from .partitions import (
    PartitionEntry,
    PartitionTable,
    extract_mtdparts_spec,
    parse_mtdparts_spec,
)
from .protocol import ParsedPath, parse_request_path
from .providers import CallableContentProvider, ContentRequest, ContentResult
from .scripted import ScriptedSessionProvider, SessionHandle
from .sessions import ClientSession, InMemorySessionStore
from .ubootops import (
    uboot_boot,
    uboot_crc32,
    uboot_download_url,
    uboot_exec_delay,
    uboot_nor_download,
    uboot_nor_probe,
)
from .ubootenv import (
    EnvPartitionInfo,
    ubootenv_build,
    ubootenv_extract,
    ubootenv_find,
    ubootenv_parse_export,
    ubootenv_parse_part,
    ubootenv_patch,
)
from .ubootscript import (
    uboot_memcpy,
    uboot_crc32_gen,
    uboot_memset,
    uboot_nor_erase,
    uboot_nor_read,
    uboot_nor_write,
    uboot_nor_gen_probe,
)
from .ubootcmds import framework_required_commands, normalize_requested_commands, build_probe_batch
from .uploads import InMemoryUploadStore, UploadedFile, UploadRequest

__all__ = [
    "CallableContentProvider",
    "ClientSession",
    "ContentRequest",
    "ContentResult",
    "DaemonConfig",
    "DownloadArtifact",
    "DownloadJobStore",
    "DownloadRequest",
    "DynamicContentServer",
    "EnvPartitionInfo",
    "GithubAsset",
    "GithubJsonManifest",
    "PartitionDigest",
    "ubootenv_build",
    "ubootenv_extract",
    "ubootenv_find",
    "InMemorySessionStore",
    "InMemoryUploadStore",
    "LegacyScriptImageCompiler",
    "PartitionEntry",
    "PartitionPayload",
    "PartitionTable",
    "PartitionUpdate",
    "PartitionUpdatePlan",
    "ParsedPath",
    "ubootenv_parse_export",
    "ubootenv_parse_part",
    "ScriptRoute",
    "ScriptedSessionProvider",
    "SessionHandle",
    "ubootenv_patch",
    "uboot_memcpy",
    "uboot_crc32",
    "uboot_crc32_gen",
    "uboot_memset",
    "uboot_boot",
    "uboot_exec_delay",
    "uboot_nor_download",
    "uboot_nor_probe",
    "uboot_nor_erase",
    "uboot_nor_read",
    "uboot_nor_write",
    "uboot_nor_gen_probe",
    "uboot_download_url",
    "UploadedFile",
    "UploadRequest",
    "build_partition_update_plan",
    "build_probe_batch",
    "collect_partition_digests",
    "extract_script_payload",
    "framework_required_commands",
    "load_daemon_config",
    "normalize_requested_commands",
    "parse_request_path",
    "partition_payload_crc32",
    "extract_mtdparts_spec",
    "parse_mtdparts_spec",
]


def __getattr__(name: str):
    if name == "DynamicContentServer":
        from .server import DynamicContentServer

        return DynamicContentServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
