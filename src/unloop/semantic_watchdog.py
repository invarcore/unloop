# Copyright 2026 Invarcore Organization
# SPDX-License-Identifier: MIT

"""Semantic Anti-Oscillation Watchdog for Agent State and Subprocess Streams.

Addresses UN-02 and STALE_REF TOCTOU drift.
Scrubs volatile runtime noise, computes RFC 8785 canonical turn digests,
and intercepts consecutive STALE_REF strikes, identical repeats, and 2-cycle oscillations.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import deque
from typing import Any

_VOLATILE_NOISE_RE = re.compile(
    r"(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})|"  # UUID
    r"(?:\b0x[0-9a-fA-F]+\b)|"  # Hex pointers
    r"(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)|"  # ISO timestamp
    r"(?:(?:timestamp|time|duration|ms|latency)[\"']?\s*[:=]\s*[\"']?[0-9.]+(?:ms|s|ns)?[\"']?)", # runtime numbers
    re.IGNORECASE,
)

_STALE_REF_RE = re.compile(
    r"\bSTALE_REF\b|@sha256:[0-9a-f]{8,64}|etag_mismatch|conflict_detected",
    re.IGNORECASE,
)


class OscillationDetectedError(RuntimeError):
    """Raised when the semantic watchdog detects a repetition death loop or oscillation."""

    def __init__(
        self,
        pattern: str,
        message: str,
        history: list[str] | None = None,
    ) -> None:
        super().__init__(f"Semantic oscillation detected ({pattern}): {message}")
        self.pattern = pattern
        self.message = message
        self.history = history or []


def _scrub_object(obj: Any) -> Any:
    """Recursively scrub volatile noise and normalize Unicode NFC in structured data."""
    if isinstance(obj, str):
        scrubbed = _VOLATILE_NOISE_RE.sub("<SCRUBBED>", obj)
        return unicodedata.normalize("NFC", scrubbed)
    elif isinstance(obj, dict):
        return {
            unicodedata.normalize("NFC", str(k)): _scrub_object(v)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [_scrub_object(x) for x in obj]
    return obj


def compute_semantic_turn_digest(data: Any) -> str:
    """Compute an RFC 8785 canonical SHA256 digest after volatile noise scrubbing.

    Guarantees that state transitions differing only by runtime timestamps,
    hex pointers, UUIDs, or latency figures hash to the identical digest.
    """
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
            scrubbed_obj = _scrub_object(parsed)
            canonical_json = json.dumps(
                scrubbed_obj,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            payload_bytes = canonical_json.encode("utf-8")
        except Exception:
            scrubbed_text = _VOLATILE_NOISE_RE.sub("<SCRUBBED>", data)
            norm_text = unicodedata.normalize("NFC", scrubbed_text)
            payload_bytes = norm_text.encode("utf-8")
    elif isinstance(data, (dict, list)):
        scrubbed_obj = _scrub_object(data)
        canonical_json = json.dumps(
            scrubbed_obj,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        payload_bytes = canonical_json.encode("utf-8")
    else:
        scrubbed_text = _VOLATILE_NOISE_RE.sub("<SCRUBBED>", str(data))
        norm_text = unicodedata.normalize("NFC", scrubbed_text)
        payload_bytes = norm_text.encode("utf-8")

    return hashlib.sha256(payload_bytes).hexdigest()


class SemanticOscillationWatchdog:
    """Monitors turns and lines for cognitive repetition loops and TOCTOU drift."""

    def __init__(
        self,
        window_size: int = 10,
        max_consecutive_identical: int = 2,
        consecutive_stale_ref_limit: int = 2,
        detect_alternating_cycles: bool = True,
    ) -> None:
        self.window_size = window_size
        self.max_consecutive_identical = max_consecutive_identical
        self.consecutive_stale_ref_limit = consecutive_stale_ref_limit
        self.detect_alternating_cycles = detect_alternating_cycles

        self._digests: deque[str] = deque(maxlen=window_size)
        self._consecutive_stale_ref_count: int = 0
        self._consecutive_identical_count: int = 0

    @property
    def history(self) -> list[str]:
        return list(self._digests)

    @property
    def stale_ref_count(self) -> int:
        return self._consecutive_stale_ref_count

    def reset(self) -> None:
        self._digests.clear()
        self._consecutive_stale_ref_count = 0
        self._consecutive_identical_count = 0

    def record_turn_content(self, content: Any) -> str:
        """Process turn content, verify oscillation invariants, and return digest.

        Raises:
            OscillationDetectedError: If consecutive STALE_REF strikes, identical
                repeats (A->A), or 2-cycle alternating loops (A->B->A->B) occur.
        """
        content_str = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        content_norm = unicodedata.normalize("NFC", content_str)

        # 1. First-class STALE_REF / TOCTOU drift detection (Guardrail 4)
        if _STALE_REF_RE.search(content_norm):
            self._consecutive_stale_ref_count += 1
            if self._consecutive_stale_ref_count >= self.consecutive_stale_ref_limit:
                raise OscillationDetectedError(
                    pattern="STALE_REF_DRIFT",
                    message=(
                        f"Consecutive STALE_REF/TOCTOU strikes detected "
                        f"({self._consecutive_stale_ref_count} consecutive matches)"
                    ),
                    history=list(self._digests),
                )
        else:
            self._consecutive_stale_ref_count = 0

        # 2. Compute canonical digest after volatile noise scrubbing (Guardrail 3)
        digest = compute_semantic_turn_digest(content)

        # 3. Check immediate consecutive repeat (A -> A)
        if self._digests and self._digests[-1] == digest:
            self._consecutive_identical_count += 1
            if self._consecutive_identical_count >= self.max_consecutive_identical:
                raise OscillationDetectedError(
                    pattern="CONSECUTIVE_REPEAT",
                    message=f"Immediate identical turn repeat detected for digest {digest}",
                    history=list(self._digests),
                )
        else:
            self._consecutive_identical_count = 1

        # 4. Check 2-cycle alternating loop (A -> B -> A -> B)
        if self.detect_alternating_cycles and len(self._digests) >= 3:
            d0 = digest
            d1 = self._digests[-1]
            d2 = self._digests[-2]
            d3 = self._digests[-3]
            if d0 == d2 and d1 == d3 and d0 != d1:
                raise OscillationDetectedError(
                    pattern="ALTERNATING_CYCLE",
                    message=(
                        f"2-cycle alternating loop detected between {d0[:8]} and {d1[:8]} "
                        f"(sequence: {d3[:8]} -> {d2[:8]} -> {d1[:8]} -> {d0[:8]})"
                    ),
                    history=list(self._digests),
                )

        self._digests.append(digest)
        return digest
