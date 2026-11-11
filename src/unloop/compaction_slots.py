# Copyright 2026 Invarcore Organization
# SPDX-License-Identifier: MIT

"""Deterministic 6-Slot Compaction Verifier for OpenClaw / Hermes schemas.

Grounded in INV-WP-2026-05 (Partitioned Invariant Compaction).
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

CANONICAL_SLOTS: tuple[str, ...] = (
    "Goal",
    "Constraints and Preferences",
    "Progress",
    "Key Decisions",
    "Next Steps",
    "Critical Context",
)

_SLOT_HEADER_RE = re.compile(
    r"^(?:#{1,4}\s*|\*\*|\[)?("
    + "|".join(re.escape(s) for s in CANONICAL_SLOTS)
    + r")(?:\*\*|\])?\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Flash Guardrail: Include ASCII hyphen (-), en-dash (\u2013), and em-dash (\u2014) in read-only/negative patterns
_NEGATIVE_INVARIANT_RE = re.compile(
    r"\b(?:never|do\s+not|don['\u2019]t|must\s+not|cannot|can['\u2019]t|forbid(?:den)?|"
    r"prohibit(?:ed)?|strictly\s+avoid|read[\-\u2013\u2014]only|immutable)\b",
    re.IGNORECASE,
)

# Captures backticked literals, CLI flags (--flag), or POSIX/Windows relative paths
_ANCHOR_TOKEN_RE = re.compile(
    r"`([^`]+)`|((?:--?[a-zA-Z0-9][\w\-]*)|(?:\b[a-zA-Z0-9_.\-]+(?:/[a-zA-Z0-9_.\-]+)+))"
)


@dataclass(frozen=True)
class SlotVerificationReport:
    passed: bool
    missing_slots: tuple[str, ...]
    dropped_constraints: tuple[str, ...]
    dropped_anchors: tuple[str, ...]
    canonical_constraints_digest: str
    slot_map: Mapping[str, str] = field(repr=False)


def parse_compaction_slots(markdown_text: str) -> dict[str, str]:
    """Parse an OpenClaw / Hermes 6-slot compaction checkpoint into a canonical map."""
    normalized_text = unicodedata.normalize("NFC", markdown_text)
    matches = list(_SLOT_HEADER_RE.finditer(normalized_text))
    parsed: dict[str, str] = {}
    canonical_lookup = {s.lower(): s for s in CANONICAL_SLOTS}

    for idx, match in enumerate(matches):
        raw_name = match.group(1).strip().lower()
        slot_name = canonical_lookup[raw_name]
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(normalized_text)
        parsed[slot_name] = normalized_text[start:end].strip()

    return parsed


def extract_hard_constraints(text: str) -> tuple[list[str], set[str]]:
    """Extract normalized negative constraint clauses and their technical anchor tokens."""
    normalized_text = unicodedata.normalize("NFC", text)
    clauses: list[str] = []
    anchors: set[str] = set()

    for raw_line in normalized_text.splitlines():
        line = re.sub(r"^[\s*\-\u2022\u2013\u2014\d.)]+", "", raw_line).strip()
        if not line:
            continue
        if _NEGATIVE_INVARIANT_RE.search(line):
            normalized_clause = re.sub(r"\s+", " ", line.lower()).rstrip(".")
            clauses.append(normalized_clause)
            for m in _ANCHOR_TOKEN_RE.finditer(line):
                token = (m.group(1) or m.group(2) or "").strip()
                if token:
                    anchors.add(token)

    return clauses, anchors


def canonicalize_constraints_rfc8785(clauses: Sequence[str], anchors: set[str]) -> str:
    """Compute an RFC 8785 deterministic SHA-256 digest of the active constraint lattice."""
    payload = {
        "anchors": sorted(unicodedata.normalize("NFC", a) for a in anchors),
        "clauses": sorted(set(unicodedata.normalize("NFC", c) for c in clauses)),
        "schema": "openclaw-6slot-v1",
    }
    canonical_bytes = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def verify_compaction_checkpoint(
    pre_compaction_text: str,
    post_compaction_text: str,
    require_all_slots: bool = True,
) -> SlotVerificationReport:
    """Deterministically verify that a 6-slot OpenClaw/Hermes compaction summary preserves constraints.

    Preserves all hard negative constraints and technical anchors (<2ms runtime).
    """
    post_slots = parse_compaction_slots(post_compaction_text)
    missing_slots = (
        tuple(s for s in CANONICAL_SLOTS if s not in post_slots) if require_all_slots else ()
    )

    pre_slots = parse_compaction_slots(pre_compaction_text)
    pre_constraint_source = pre_slots.get("Constraints and Preferences", pre_compaction_text)
    post_constraint_source = (
        post_slots.get("Constraints and Preferences", "")
        + "\n"
        + post_slots.get("Critical Context", "")
    )

    pre_clauses, pre_anchors = extract_hard_constraints(pre_constraint_source)
    post_clauses, post_anchors = extract_hard_constraints(post_constraint_source)

    post_text_lower = post_constraint_source.lower()
    dropped_anchors = tuple(
        sorted(a for a in pre_anchors if a not in post_anchors and a.lower() not in post_text_lower)
    )

    post_clause_set = set(post_clauses)
    dropped_constraints: list[str] = []
    for clause in pre_clauses:
        clause_anchors = {
            (m.group(1) or m.group(2) or "").strip()
            for m in _ANCHOR_TOKEN_RE.finditer(clause)
            if (m.group(1) or m.group(2))
        }
        if clause not in post_clause_set:
            if not clause_anchors or not clause_anchors.issubset(post_anchors):
                dropped_constraints.append(clause)

    digest = canonicalize_constraints_rfc8785(post_clauses, post_anchors)
    passed = len(missing_slots) == 0 and len(dropped_constraints) == 0 and len(dropped_anchors) == 0

    return SlotVerificationReport(
        passed=passed,
        missing_slots=missing_slots,
        dropped_constraints=tuple(dropped_constraints),
        dropped_anchors=dropped_anchors,
        canonical_constraints_digest=digest,
        slot_map=post_slots,
    )


_DRIVE_RE = re.compile(r"^([A-Za-z]:)(/.*)?$")


def _normalize_arcbench_path(raw: str) -> str:
    decoded = unquote(raw)
    has_bs = chr(92) in raw or chr(92) in decoded
    forward = decoded.replace(chr(92), "/").strip()
    is_drive = bool(_DRIVE_RE.match(forward))
    collapsed = re.sub(r"/+", "/", forward)
    norm = posixpath.normpath(collapsed) if collapsed else ""
    if collapsed.startswith("/") and not norm.startswith("/"):
        norm = "/" + norm.lstrip("/")
    if norm == "." and not raw.strip().startswith("."):
        norm = ""
    if len(norm) > 1 and norm.endswith("/") and not _DRIVE_RE.match(norm):
        norm = norm.rstrip("/")
    if norm.endswith(":"):
        norm = norm + "/"
    return norm.casefold() if (is_drive or has_bs) else norm


def _normalize_jcs(obj: Any) -> Any:
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ValueError("RFC 8785 canonical JSON cannot encode NaN or Infinity.")
        if obj == 0.0:
            return 0
        if obj.is_integer():
            return int(obj)
        return obj
    if isinstance(obj, Mapping):
        return {unicodedata.normalize("NFC", str(k)): _normalize_jcs(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize_jcs(x) for x in obj]
    return obj


def export_arcbench_lock_sidecar(
    compaction_epoch: int,
    post_compaction_text: str = "",
    *,
    allowed_tools: Sequence[str] = (),
    forbidden_tools: Sequence[str] = (),
    forbidden_path_prefixes: Sequence[str] = (),
    pinned_arg_literals: Mapping[str, Mapping[str, Any]] | None = None,
    pinned_model_version: str | None = None,
    extract_path_anchors: bool = True,
) -> dict[str, Any]:
    """Export an RFC 8785 @arcbench_lock:sha256 sidecar dictionary for intent-fabric."""
    extracted_paths: list[str] = list(forbidden_path_prefixes)
    if extract_path_anchors and post_compaction_text:
        slots = parse_compaction_slots(post_compaction_text)
        source = (
            slots.get("Constraints and Preferences", "") + "\n" + slots.get("Critical Context", "")
        ).strip() or post_compaction_text
        _, anchors = extract_hard_constraints(source)
        for anchor in sorted(anchors):
            if "/" in anchor or chr(92) in anchor:
                extracted_paths.append(anchor)

    norm_allowed = sorted(set(allowed_tools))
    norm_tools = sorted(set(forbidden_tools))
    norm_paths = sorted({_normalize_arcbench_path(p) for p in extracted_paths if p})
    norm_args = (
        {str(k): dict(sorted(v.items())) for k, v in sorted(pinned_arg_literals.items())}
        if pinned_arg_literals
        else {}
    )
    payload = {
        "allowed_tools": norm_allowed,
        "compaction_epoch": compaction_epoch,
        "forbidden_path_prefixes": norm_paths,
        "forbidden_tools": norm_tools,
        "pinned_arg_literals": norm_args,
        "pinned_model_version": pinned_model_version,
    }
    canonical = json.dumps(
        _normalize_jcs(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    digest = "@arcbench_lock:sha256:" + hashlib.sha256(canonical).hexdigest()
    return {
        "schema_version": "arcbench-lock-v1",
        **payload,
        "lock_digest": digest,
    }
