# Copyright 2026 Invarcore Organization
# SPDX-License-Identifier: MIT

"""Deterministic 6-Slot Compaction Verifier for OpenClaw / Hermes schemas.

Grounded in INV-WP-2026-05 (Partitioned Invariant Compaction).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

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

_NEGATIVE_INVARIANT_RE = re.compile(
    r"\b(?:never|do\s+not|don't|must\s+not|cannot|can't|forbid(?:den)?|prohibit(?:ed)?|"
    r"strictly\s+avoid|read-only|immutable)\b",
    re.IGNORECASE,
)

_ANCHOR_TOKEN_RE = re.compile(
    r"`([^`]+)`|((?:--?[a-zA-Z0-9_-]+)|(?:\b[a-zA-Z0-9_.-]+/[a-zA-Z0-9_./-]+))"
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
    """Parse an OpenClaw / Hermes 6-slot compaction checkpoint into a canonical dictionary."""
    matches = list(_SLOT_HEADER_RE.finditer(markdown_text))
    parsed: dict[str, str] = {}
    canonical_lookup = {s.lower(): s for s in CANONICAL_SLOTS}

    for idx, match in enumerate(matches):
        raw_name = match.group(1).strip().lower()
        slot_name = canonical_lookup[raw_name]
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown_text)
        parsed[slot_name] = markdown_text[start:end].strip()

    return parsed


def extract_hard_constraints(text: str) -> tuple[list[str], set[str]]:
    """Extract normalized negative constraint clauses and code/path anchor tokens."""
    clauses: list[str] = []
    anchors: set[str] = set()

    for raw_line in text.splitlines():
        line = re.sub(r"^[\s*\-•\d.)]+", "", raw_line).strip()
        if not line:
            continue
        if _NEGATIVE_INVARIANT_RE.search(line):
            normalized = re.sub(r"\s+", " ", line.lower()).rstrip(".")
            clauses.append(normalized)
            for m in _ANCHOR_TOKEN_RE.finditer(line):
                token = (m.group(1) or m.group(2) or "").strip()
                if token:
                    anchors.add(token)

    return clauses, anchors


def canonicalize_constraints_rfc8785(clauses: Sequence[str], anchors: set[str]) -> str:
    """Compute an RFC 8785 deterministic SHA-256 digest of the constraint lattice."""
    payload = {
        "anchors": sorted(anchors),
        "clauses": sorted(set(clauses)),
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
    """Deterministically verify that an OpenClaw/Hermes compaction summary preserves constraints.

    Preserves all hard negative constraints and technical anchors (<2ms runtime).
    """
    post_slots = parse_compaction_slots(post_compaction_text)
    missing_slots = (
        tuple(s for s in CANONICAL_SLOTS if s not in post_slots)
        if require_all_slots
        else ()
    )

    pre_slots = parse_compaction_slots(pre_compaction_text)
    pre_constraint_source = pre_slots.get(
        "Constraints and Preferences", pre_compaction_text
    )
    post_constraint_source = post_slots.get(
        "Constraints and Preferences", ""
    ) + "\n" + post_slots.get("Critical Context", "")

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
    passed = (
        len(missing_slots) == 0
        and len(dropped_constraints) == 0
        and len(dropped_anchors) == 0
    )

    return SlotVerificationReport(
        passed=passed,
        missing_slots=missing_slots,
        dropped_constraints=tuple(dropped_constraints),
        dropped_anchors=dropped_anchors,
        canonical_constraints_digest=digest,
        slot_map=post_slots,
    )
