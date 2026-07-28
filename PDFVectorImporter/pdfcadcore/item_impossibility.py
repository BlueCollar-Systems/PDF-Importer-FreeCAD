"""Host-neutral, item-bound seal for one impossible representation attempt.

Host adapters retain their own proof-family validators.  This module only seals
the exact branch evidence and entity lifecycle after that host validation has
succeeded, so canonical report consumers can detect mutation or substitution.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Optional, Set


SCHEMA = "bcs.item_representation_impossibility/1.0"
EVIDENCE_KEY = "item_representation_impossibility_proof"
PROOF_FIELDS = (
    "schema",
    "source_item_id",
    "requested_type",
    "attempted_type",
    "strategy",
    "reason",
    "host_outcome",
    "branch_evidence_sha256",
    "cleanup_complete",
    "created_entity_ids",
    "removed_entity_ids",
    "delivery_entity_ids",
    "support_entity_ids",
    "referenced_entity_ids",
    "reused_entity_ids",
    "owned_block_names",
    "proof_sha256",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _validate_json_value(
    value: Any,
    path: str = "value",
    seen: Optional[Set[int]] = None,
) -> None:
    if seen is None:
        seen = set()
    if value is None or type(value) in {bool, int, str}:
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        marker = id(value)
        if marker in seen:
            raise ValueError(f"{path} contains a cycle")
        seen.add(marker)
        try:
            for index, item in enumerate(value):
                _validate_json_value(item, f"{path}[{index}]", seen)
        finally:
            seen.remove(marker)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError(f"{path} contains a non-string object key")
        marker = id(value)
        if marker in seen:
            raise ValueError(f"{path} contains a cycle")
        seen.add(marker)
        try:
            for key, item in value.items():
                _validate_json_value(item, f"{path}.{key}", seen)
        finally:
            seen.remove(marker)
        return
    raise TypeError(f"{path} is not a strict JSON value")


def _canonical_json_bytes(value: Any) -> bytes:
    _validate_json_value(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _exact_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be an exact nonempty string")
    return value


def _exact_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a JSON list")
    result = [_exact_text(item, f"{field}[]") for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must contain unique identities")
    return sorted(result)


def build_item_representation_impossibility_proof(
    *,
    branch_evidence: dict[str, Any],
    source_item_id: str,
    requested_type: str,
    attempted_type: str,
    strategy: str,
    reason: str,
    host_outcome: str,
    cleanup_complete: bool,
    created_entity_ids: list[str],
    removed_entity_ids: list[str],
    delivery_entity_ids: list[str],
    support_entity_ids: list[str],
    referenced_entity_ids: list[str],
    reused_entity_ids: list[str],
    owned_block_names: list[str],
) -> dict[str, Any]:
    """Build a strict seal or raise; never return a partial proof."""

    if not isinstance(branch_evidence, dict):
        raise TypeError("branch_evidence must be a JSON object")
    if EVIDENCE_KEY in branch_evidence:
        raise ValueError("branch_evidence already contains the reserved seal key")
    _validate_json_value(branch_evidence, "branch_evidence")

    source_id = _exact_text(source_item_id, "source_item_id")
    requested = _exact_text(requested_type, "requested_type")
    attempted = _exact_text(attempted_type, "attempted_type")
    strategy_value = _exact_text(strategy, "strategy")
    reason_value = _exact_text(reason, "reason")
    outcome = _exact_text(host_outcome, "host_outcome")
    if outcome != "impossible":
        raise ValueError("host_outcome must be exactly 'impossible'")
    if cleanup_complete is not True:
        raise ValueError("cleanup_complete must be exactly true")

    created = _exact_string_list(created_entity_ids, "created_entity_ids")
    removed = _exact_string_list(removed_entity_ids, "removed_entity_ids")
    delivered = _exact_string_list(delivery_entity_ids, "delivery_entity_ids")
    supported = _exact_string_list(support_entity_ids, "support_entity_ids")
    referenced = _exact_string_list(referenced_entity_ids, "referenced_entity_ids")
    reused = _exact_string_list(reused_entity_ids, "reused_entity_ids")
    owned_blocks = _exact_string_list(owned_block_names, "owned_block_names")

    if set(created) != set(removed):
        raise ValueError("created and removed identities do not reconcile")
    if delivered or supported or reused:
        raise ValueError("an impossible branch cannot retain delivered entities")
    if set(referenced).intersection(set(created).union(removed)):
        raise ValueError("referenced identities overlap owned lifecycle identities")

    proof = {
        "schema": SCHEMA,
        "source_item_id": source_id,
        "requested_type": requested,
        "attempted_type": attempted,
        "strategy": strategy_value,
        "reason": reason_value,
        "host_outcome": outcome,
        "branch_evidence_sha256": _digest(branch_evidence),
        "cleanup_complete": True,
        "created_entity_ids": created,
        "removed_entity_ids": removed,
        "delivery_entity_ids": delivered,
        "support_entity_ids": supported,
        "referenced_entity_ids": referenced,
        "reused_entity_ids": reused,
        "owned_block_names": owned_blocks,
    }
    proof["proof_sha256"] = _digest(proof)
    return proof


def item_representation_impossibility_proof_verified(
    proof: Any,
    *,
    branch_evidence: dict[str, Any],
    source_item_id: str,
    requested_type: str,
    attempted_type: str,
    strategy: str,
    reason: str,
    host_outcome: str,
    cleanup_complete: bool,
    created_entity_ids: list[str],
    removed_entity_ids: list[str],
    delivery_entity_ids: list[str],
    support_entity_ids: list[str],
    referenced_entity_ids: list[str],
    reused_entity_ids: list[str],
    owned_block_names: list[str],
) -> bool:
    """Verify against independently held branch values; total and fail-closed."""

    try:
        if not isinstance(proof, dict) or set(proof) != set(PROOF_FIELDS):
            return False
        if proof.get("schema") != SCHEMA:
            return False
        digest = proof.get("proof_sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            return False
        expected = build_item_representation_impossibility_proof(
            branch_evidence=branch_evidence,
            source_item_id=source_item_id,
            requested_type=requested_type,
            attempted_type=attempted_type,
            strategy=strategy,
            reason=reason,
            host_outcome=host_outcome,
            cleanup_complete=cleanup_complete,
            created_entity_ids=created_entity_ids,
            removed_entity_ids=removed_entity_ids,
            delivery_entity_ids=delivery_entity_ids,
            support_entity_ids=support_entity_ids,
            referenced_entity_ids=referenced_entity_ids,
            reused_entity_ids=reused_entity_ids,
            owned_block_names=owned_block_names,
        )
        return _canonical_json_bytes(proof) == _canonical_json_bytes(expected)
    except Exception:
        return False


__all__ = [
    "SCHEMA",
    "EVIDENCE_KEY",
    "PROOF_FIELDS",
    "build_item_representation_impossibility_proof",
    "item_representation_impossibility_proof_verified",
]
