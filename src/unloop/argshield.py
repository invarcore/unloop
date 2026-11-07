# Copyright 2026 Invarcore Organization
# SPDX-License-Identifier: MIT

"""ArgShield: Deterministic Argument Sanitization and Injection Defense for MCP.

Addresses CVE-2026-85626 (git option injection), CVE-2026-77262, and OWASP ASI04.
Enforces strict path jailing via os.path.commonpath and targeted CLI flag injection defense.
"""

from __future__ import annotations

import os
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BANNED_FLAG_PREFIXES: tuple[str, ...] = (
    "--output=",
    "--upload-pack=",
    "--exec=",
    "--config=",
    "--git-dir=",
)

STRICT_PATH_KEYS: frozenset[str] = frozenset({
    "path",
    "filepath",
    "filename",
    "dir",
    "directory",
    "cwd",
    "worktree",
    "repo_path",
})

POSITIONAL_FLAG_PARAMS: frozenset[str] = frozenset({
    "ref",
    "object",
    "revision",
    "branch",
    "tag",
    "commit",
})

WIN_DEVICE_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class ArgShieldViolationError(ValueError):
    """Raised when a tool argument violates ArgShield security policies."""

    def __init__(self, field: str, violation_type: str, message: str) -> None:
        super().__init__(f"ArgShield violation on {field} ({violation_type}): {message}")
        self.field = field
        self.violation_type = violation_type
        self.message = message


@dataclass(frozen=True)
class ArgShieldPolicy:
    """Security policy configuration for ArgShield argument sanitization."""

    jail_dir: Path | str | None = None
    banned_flag_prefixes: tuple[str, ...] = BANNED_FLAG_PREFIXES
    strict_path_keys: frozenset[str] = STRICT_PATH_KEYS
    positional_flag_param_names: frozenset[str] = POSITIONAL_FLAG_PARAMS
    disallow_null_bytes: bool = True

    @classmethod
    def from_tool_schema(
        cls,
        tools: list[dict[str, Any]],
        jail_dir: Path | str | None = None,
        base_policy: ArgShieldPolicy | None = None,
    ) -> ArgShieldPolicy:
        """Dynamically learn and enrich strict path keys from MCP tools/list schemas."""
        inferred_keys = set(base_policy.strict_path_keys if base_policy else STRICT_PATH_KEYS)
        for tool in tools:
            schema = tool.get("inputSchema", {})
            properties = schema.get("properties", {})
            if isinstance(properties, dict):
                for prop_name, prop_meta in properties.items():
                    if isinstance(prop_meta, dict):
                        prop_fmt = prop_meta.get("format", "").lower()
                        desc = prop_meta.get("description", "").lower()
                        if prop_fmt in ("path", "uri", "filepath") or any(
                            kw in desc for kw in ("file path", "directory path", "filesystem path", "relative path")
                        ):
                            inferred_keys.add(prop_name.lower())
        return cls(
            jail_dir=jail_dir or (base_policy.jail_dir if base_policy else None),
            strict_path_keys=frozenset(inferred_keys),
            banned_flag_prefixes=base_policy.banned_flag_prefixes if base_policy else BANNED_FLAG_PREFIXES,
            positional_flag_param_names=(
                base_policy.positional_flag_param_names if base_policy else POSITIONAL_FLAG_PARAMS
            ),
            disallow_null_bytes=base_policy.disallow_null_bytes if base_policy else True,
        )


def verify_path_in_jail(
    raw_path: str | Path,
    jail_dir: Path | str,
    field_name: str = "path",
) -> Path:
    """Verify that raw_path resolves strictly within jail_dir.

    Guards against path traversal, null byte injections, prefix-sibling escapes,
    and Windows DOS device namespace hijacks (CON, PRN, AUX, NUL).
    Uses os.path.commonpath for strict boundary verification.
    """
    norm_raw = unicodedata.normalize("NFC", str(raw_path))
    if "\x00" in norm_raw:
        raise ArgShieldViolationError(
            field=field_name,
            violation_type="null_byte",
            message="Null byte detected in path parameter",
        )

    jail_path = Path(jail_dir).resolve()
    target_path = (jail_path / norm_raw).resolve()

    # Defend against Windows DOS reserved device names (CON, NUL, AUX, COM1-9, etc.)
    if sys.platform == "win32" or os.name == "nt":
        target_stem = target_path.stem.upper()
        target_name = target_path.name.upper()
        if target_stem in WIN_DEVICE_NAMES or target_name in WIN_DEVICE_NAMES:
            raise ArgShieldViolationError(
                field=field_name,
                violation_type="device_name",
                message=f"Path targets Windows reserved device name: {target_path.name}",
            )

    try:
        common = os.path.commonpath([str(jail_path), str(target_path)])
    except ValueError as err:
        raise ArgShieldViolationError(
            field=field_name,
            violation_type="path_traversal",
            message=f"Path {target_path} is outside jail volume: {err}",
        ) from err

    if common != str(jail_path):
        raise ArgShieldViolationError(
            field=field_name,
            violation_type="path_traversal",
            message=f"Path {target_path} escapes jail {jail_path}",
        )

    return target_path


def verify_no_flag_injection(
    param_name: str,
    val: str,
    policy: ArgShieldPolicy | None = None,
) -> None:
    """Verify that string parameter val does not contain forbidden CLI flags.

    Defends against CVE-2026-85626 (git option injection).
    Blocks banned flag prefixes in both equals-delimited (--flag=val) and
    space-delimited (--flag val) formats, as well as leading dashes on positional parameters.
    """
    banned_prefixes = policy.banned_flag_prefixes if policy else BANNED_FLAG_PREFIXES
    positional_params = policy.positional_flag_param_names if policy else POSITIONAL_FLAG_PARAMS

    val_norm = unicodedata.normalize("NFC", str(val))
    if val_norm.startswith("-"):
        for banned in banned_prefixes:
            stem = banned.rstrip("=")
            if val_norm == stem or val_norm.startswith(banned) or val_norm.startswith(stem + " "):
                raise ArgShieldViolationError(
                    field=param_name,
                    violation_type="flag_injection",
                    message=f"Banned flag {banned} detected in {param_name}: {val_norm}",
                )
        if param_name.lower() in positional_params:
            raise ArgShieldViolationError(
                field=param_name,
                violation_type="flag_injection",
                message=f"Positional parameter {param_name} cannot start with dash: {val_norm}",
            )


def sanitize_tool_arguments(
    tool_name: str,
    arguments: dict[str, Any] | None,
    jail_dir: Path | str | None = None,
    policy: ArgShieldPolicy | None = None,
) -> dict[str, Any]:
    """Sanitize and validate arguments for an MCP tool invocation.

    Restricts path jailing strictly to STRICT_PATH_KEYS (Guardrail 1).
    Validates CLI flags without global dash bans (Guardrail 2).
    """
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        return arguments

    pol = policy or ArgShieldPolicy(jail_dir=jail_dir)
    effective_jail = pol.jail_dir or jail_dir
    sanitized: dict[str, Any] = {}

    for k, v in arguments.items():
        k_norm = unicodedata.normalize("NFC", str(k))
        if isinstance(v, str):
            verify_no_flag_injection(k_norm, v, policy=pol)
            if k_norm.lower() in pol.strict_path_keys and effective_jail is not None:
                sanitized[k_norm] = str(verify_path_in_jail(v, effective_jail, field_name=k_norm))
            else:
                sanitized[k_norm] = unicodedata.normalize("NFC", v)
        elif isinstance(v, dict):
            sanitized[k_norm] = sanitize_tool_arguments(
                tool_name=tool_name,
                arguments=v,
                jail_dir=effective_jail,
                policy=pol,
            )
        elif isinstance(v, list):
            sanitized_list = []
            for item in v:
                if isinstance(item, str):
                    verify_no_flag_injection(k_norm, item, policy=pol)
                    sanitized_list.append(unicodedata.normalize("NFC", item))
                elif isinstance(item, dict):
                    sanitized_list.append(
                        sanitize_tool_arguments(
                            tool_name=tool_name,
                            arguments=item,
                            jail_dir=effective_jail,
                            policy=pol,
                        )
                    )
                else:
                    sanitized_list.append(item)
            sanitized[k_norm] = sanitized_list
        else:
            sanitized[k_norm] = v

    return sanitized
