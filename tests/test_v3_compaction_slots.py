from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata
from typing import Any

import pytest

from unloop.compaction_slots import verify_compaction_checkpoint

TEST_SECRET_KEY = "invarcore-v3-unloop-hmac-secret-key-32bytes-min!"
WRONG_SECRET_KEY = "wrong-v3-unloop-hmac-secret-key-32bytes-minimum!"


def _canonicalize_nfc(val: Any) -> Any:
    if isinstance(val, str):
        return unicodedata.normalize("NFC", val)
    if isinstance(val, bool) or val is None or isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val) if val.is_integer() else val
    if isinstance(val, dict):
        items = sorted(
            val.items(),
            key=lambda item: unicodedata.normalize("NFC", str(item[0])),
        )
        return {unicodedata.normalize("NFC", str(k)): _canonicalize_nfc(v) for k, v in items}
    if isinstance(val, (list, tuple)):
        return [_canonicalize_nfc(x) for x in val]
    return val


def _sign_sidecar(sidecar_body: dict[str, Any], secret_key: str) -> dict[str, Any]:
    raw_pinned = sidecar_body.get("pinned_arg_literals") or {}
    typed_pinned: dict[str, dict[str, dict[str, Any]]] = {}
    for k, v in sorted(raw_pinned.items(), key=lambda item: str(item[0])):
        typed_pinned[str(k)] = {
            str(ak): {
                "type": "list" if isinstance(av, (list, tuple)) else type(av).__name__,
                "value": list(av) if isinstance(av, (list, tuple)) else av,
            }
            for ak, av in sorted(v.items(), key=lambda item: str(item[0]))
        }
    allow_any = bool(sidecar_body.get("allow_any_tool", False))
    payload = {
        "schema": "arcbench-compaction-lock-v3",
        "allow_any_tool": allow_any,
        "allowed_tools": list(sidecar_body.get("allowed_tools", ())),
        "forbidden_tools": list(sidecar_body.get("forbidden_tools", ())),
        "forbidden_path_prefixes": list(sidecar_body.get("forbidden_path_prefixes", ())),
        "pinned_arg_literals": typed_pinned,
        "pinned_model_version": sidecar_body.get("pinned_model_version"),
    }
    canon = json.dumps(
        _canonicalize_nfc(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    mac = hmac.new(secret_key.encode("utf-8"), canon, hashlib.sha256).hexdigest()
    return {
        **sidecar_body,
        "schema": "arcbench-compaction-lock-v3",
        "allow_any_tool": allow_any,
        "lock_digest": f"@arcbench_lock:hmac-sha256:{mac}",
    }


def test_checkpoint_fails_closed_when_arcbench_sidecar_is_stripped() -> None:
    stripped_checkpoint = {
        "schema": "openclaw-6slot-v1",
        "slots": {"Goal": "deploy", "Constraints and Preferences": "never touch /etc"},
    }
    with pytest.raises(ValueError):
        verify_compaction_checkpoint(stripped_checkpoint, signing_key=TEST_SECRET_KEY)

    assert verify_compaction_checkpoint(stripped_checkpoint, require_arcbench_lock=False) is True


def test_checkpoint_rejects_forged_wrong_key_short_key_and_allow_all_flip() -> None:
    body = {
        "allow_any_tool": False,
        "allowed_tools": ["read_file"],
        "forbidden_tools": ["shell_exec"],
        "forbidden_path_prefixes": ["/etc/"],
        "pinned_arg_literals": {"read_file": {"dry_run": True}},
        "pinned_model_version": "gemini-2.5-pro",
    }
    signed = _sign_sidecar(body, TEST_SECRET_KEY)
    checkpoint = {"schema": "openclaw-6slot-v1", "arcbench_lock": signed}

    assert verify_compaction_checkpoint(checkpoint, signing_key=TEST_SECRET_KEY) is True

    for bad_key in (None, "", "short-key"):
        with pytest.raises(ValueError):
            verify_compaction_checkpoint(checkpoint, signing_key=bad_key)

    with pytest.raises(ValueError):
        verify_compaction_checkpoint(checkpoint, signing_key=WRONG_SECRET_KEY)

    forged = dict(signed)
    forged["lock_digest"] = "@arcbench_lock:sha256:" + hashlib.sha256(b"forged").hexdigest()
    with pytest.raises(ValueError):
        verify_compaction_checkpoint(
            {"schema": "openclaw-6slot-v1", "arcbench_lock": forged},
            signing_key=TEST_SECRET_KEY,
        )

    flipped = dict(signed)
    flipped["allow_any_tool"] = True
    with pytest.raises(ValueError):
        verify_compaction_checkpoint(
            {"schema": "openclaw-6slot-v1", "arcbench_lock": flipped},
            signing_key=TEST_SECRET_KEY,
        )

    with pytest.raises(ValueError):
        verify_compaction_checkpoint(
            {"schema": "openclaw-6slot-v1", "arcbench_lock": "bypass"},
            signing_key=TEST_SECRET_KEY,
        )
