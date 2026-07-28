"""Shared, fail-closed text-delivery report projection and resolver.

The full item-attempt evidence belongs in ``extra.text_delivery_attempts``
exactly once.  ``text_representation_delivery`` is deliberately a compact
index over that ledger; it is not a second copy of host evidence.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


SCHEMA = "bcs.text_representation_delivery/1.1"
DELIVERY_FIELDS = (
    "schema",
    "required",
    "requested_type",
    "verified",
    "attempt_count",
    "source_item_count",
    "delivered_item_count",
    "failed_item_count",
    "items",
    "invalid_reasons",
)

# The shared resolver has to remain useful before a caller knows which host
# produced a ledger, but it must never invent a shortcut.  These are the
# deduplicated full ladders supported by the three first-party adapters.  A
# host-aware caller should pass its one closed ladder with ``fallback_ladders``
# so a sequence accepted for another host cannot leak into its contract.
CANONICAL_FALLBACK_LADDERS: dict[str, tuple[tuple[str, ...], ...]] = {
    "labels": (
        ("labels", "text", "3d_text", "glyphs", "geometry", "raster"),
        ("labels", "text", "glyphs", "geometry", "raster"),
    ),
    "text": (
        ("text", "labels", "3d_text", "glyphs", "geometry", "raster"),
        ("text", "3d_text", "glyphs", "geometry", "raster"),
        ("text", "glyphs", "geometry", "raster"),
    ),
    "3d_text": (
        ("3d_text", "glyphs", "geometry", "text", "labels", "raster"),
        ("3d_text", "text", "glyphs", "geometry", "raster"),
    ),
    "glyphs": (
        ("glyphs", "geometry", "3d_text", "text", "labels", "raster"),
        ("glyphs", "geometry", "raster"),
        ("glyphs", "geometry", "text", "raster"),
    ),
    "geometry": (
        ("geometry", "glyphs", "3d_text", "text", "labels", "raster"),
        ("geometry", "glyphs", "raster"),
        ("geometry", "glyphs", "text", "raster"),
    ),
    "raster": (("raster",),),
}


def _exact_text(value: Any) -> tuple[str, bool]:
    if (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
    ):
        return value, True
    return "", False


def _text(value: Any) -> str:
    return _exact_text(value)[0]


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _string_ids(value: Any) -> tuple[list[str], bool]:
    if not isinstance(value, (list, tuple)):
        return [], False
    ids: list[str] = []
    valid = True
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
        ):
            valid = False
        ids.append(item if isinstance(item, str) else "")
    return ids, bool(valid and len(ids) == len(set(ids)))


def _has_attempt_evidence(attempt: Mapping[str, Any]) -> bool:
    return any(
        isinstance(attempt.get(field), Mapping) and bool(attempt.get(field))
        for field in ("evidence", "proof")
    )


def _fallback_ladder_candidates(
    requested_type: str,
    fallback_ladders: Mapping[str, Any] | None,
) -> tuple[tuple[str, ...], ...]:
    """Return exact, acyclic ladder candidates for one requested type."""

    if fallback_ladders is None:
        return CANONICAL_FALLBACK_LADDERS.get(requested_type, ())
    if not isinstance(fallback_ladders, Mapping):
        return ()
    raw = fallback_ladders.get(requested_type)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        return ()
    raw_candidates: Sequence[Any]
    if all(isinstance(item, str) for item in raw):
        raw_candidates = (raw,)
    else:
        raw_candidates = raw
    candidates: list[tuple[str, ...]] = []
    for raw_candidate in raw_candidates:
        if (
            not isinstance(raw_candidate, Sequence)
            or isinstance(raw_candidate, (str, bytes))
            or not raw_candidate
        ):
            return ()
        candidate = tuple(raw_candidate)
        if (
            any(not _exact_text(rung)[1] for rung in candidate)
            or candidate[0] != requested_type
            or len(candidate) != len(set(candidate))
        ):
            return ()
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def _exact_json_equal(actual: Any, expected: Any) -> bool:
    """Compare JSON values recursively without Python's bool/int coercions."""

    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return bool(
            all(type(key) is str for key in actual)
            and set(actual) == set(expected)
            and all(_exact_json_equal(actual[key], expected[key]) for key in expected)
        )
    if type(expected) is list:
        return bool(
            len(actual) == len(expected)
            and all(
                _exact_json_equal(actual_item, expected_item)
                for actual_item, expected_item in zip(actual, expected, strict=True)
            )
        )
    if type(expected) is float:
        return bool(math.isfinite(actual) and math.isfinite(expected) and actual == expected)
    if type(expected) in {type(None), bool, int, str}:
        return actual == expected
    return False


def _project(
    attempts: Sequence[Mapping[str, Any]],
    *,
    requested_type: str,
    required: bool,
    expected_source_item_ids: Iterable[str] | None,
    fallback_ladders: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
    requested, requested_valid = _exact_text(requested_type)
    reasons: list[str] = []
    if not requested_valid:
        _append_reason(reasons, "requested_type is not an exact nonempty string")

    source_indexes: dict[str, list[int]] = {}
    source_order: list[str] = []
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, Mapping):
            _append_reason(reasons, f"attempt[{index}] is not an object")
            continue
        source_id, source_id_valid = _exact_text(attempt.get("source_item_id"))
        if not source_id_valid:
            _append_reason(
                reasons,
                f"attempt[{index}].source_item_id is not an exact nonempty string",
            )
            continue
        if source_id not in source_indexes:
            source_indexes[source_id] = []
            source_order.append(source_id)
        source_indexes[source_id].append(index)

        attempt_requested, attempt_requested_valid = _exact_text(
            attempt.get("requested_type")
        )
        if not attempt_requested_valid:
            _append_reason(
                reasons,
                f"attempt[{index}].requested_type is not an exact nonempty string",
            )
        elif attempt_requested != requested:
            _append_reason(reasons, f"attempt[{index}].requested_type mismatch")
        _, attempted_type_valid = _exact_text(attempt.get("attempted_type"))
        if not attempted_type_valid:
            _append_reason(
                reasons,
                f"attempt[{index}].attempted_type is not an exact nonempty string",
            )
        outcome, outcome_valid = _exact_text(attempt.get("outcome"))
        if not outcome_valid or outcome not in {
            "proven_impossible",
            "verified",
            "failed",
        }:
            _append_reason(reasons, f"attempt[{index}].outcome is invalid")
        for field in (
            "created_entity_ids",
            "removed_entity_ids",
            "delivery_entity_ids",
            "support_entity_ids",
            "referenced_entity_ids",
            "reused_entity_ids",
        ):
            _, valid_ids = _string_ids(attempt.get(field))
            if not valid_ids:
                _append_reason(reasons, f"attempt[{index}].{field} is invalid")
        if not any(
            isinstance(attempt.get(field), Mapping)
            for field in ("evidence", "proof")
        ):
            _append_reason(
                reasons,
                f"attempt[{index}] has no evidence or proof object",
            )

    actual_source_ids = set(source_indexes)
    if expected_source_item_ids is not None:
        expected_values = list(expected_source_item_ids)
        expected_pairs = [_exact_text(item) for item in expected_values]
        expected_list = [item for item, _ in expected_pairs]
        expected_ids = {item for item, valid in expected_pairs if valid}
        if not all(valid for _, valid in expected_pairs):
            _append_reason(
                reasons,
                "expected_source_item_ids contain an invalid identity",
            )
        if (
            len(expected_list) != len(expected_ids)
            or actual_source_ids != expected_ids
        ):
            _append_reason(reasons, "source_item_ids do not match expected source set")

    items: list[dict[str, Any]] = []
    terminals: list[Mapping[str, Any]] = []
    delivered_count = 0
    for source_id in source_order:
        indexes = source_indexes[source_id]
        terminal_index = indexes[-1]
        terminal = attempts[terminal_index]
        terminals.append(terminal)
        item_reasons: list[str] = []

        attempted_sequence = [
            _text(attempts[index].get("attempted_type")) for index in indexes
        ]
        ladder_candidates = _fallback_ladder_candidates(requested, fallback_ladders)
        if not attempted_sequence or attempted_sequence[0] != requested:
            item_reasons.append(
                f"source {source_id} attempts do not start with requested_type"
            )
        if len(attempted_sequence) != len(set(attempted_sequence)):
            item_reasons.append(
                f"source {source_id} fallback ladder repeats an attempted_type"
            )
        if not any(
            tuple(attempted_sequence) == ladder[: len(attempted_sequence)]
            for ladder in ladder_candidates
        ):
            item_reasons.append(
                f"source {source_id} does not follow an exact fallback ladder prefix"
            )

        for prior_index in indexes[:-1]:
            prior = attempts[prior_index]
            if _text(prior.get("outcome")) != "proven_impossible":
                item_reasons.append(
                    f"source {source_id} attempt[{prior_index}] lacks impossibility proof"
                )
            if prior.get("cleanup_complete") is not True:
                item_reasons.append(
                    f"source {source_id} attempt[{prior_index}] cleanup is incomplete"
                )
            if not _has_attempt_evidence(prior):
                item_reasons.append(
                    f"source {source_id} attempt[{prior_index}] evidence is empty"
                )
            created_ids = set(_string_ids(prior.get("created_entity_ids"))[0])
            removed_ids = set(_string_ids(prior.get("removed_entity_ids"))[0])
            if created_ids != removed_ids:
                item_reasons.append(
                    f"source {source_id} attempt[{prior_index}] cleanup ownership mismatch"
                )
            if _string_ids(prior.get("delivery_entity_ids"))[0] or _string_ids(
                prior.get("support_entity_ids")
            )[0] or _string_ids(prior.get("reused_entity_ids"))[0]:
                item_reasons.append(
                    f"source {source_id} attempt[{prior_index}] retains delivery entities"
                )

        terminal_outcome = _text(terminal.get("outcome"))
        terminal_attempted = _text(terminal.get("attempted_type"))
        terminal_final = _text(terminal.get("final_type"))
        delivery_ids, delivery_ids_valid = _string_ids(
            terminal.get("delivery_entity_ids")
        )
        if terminal_outcome != "verified":
            item_reasons.append(
                f"source {source_id} terminal attempt is not verified"
            )
        if terminal.get("cleanup_complete") is not True:
            item_reasons.append(
                f"source {source_id} terminal cleanup is incomplete"
            )
        if not terminal_final or terminal_final != terminal_attempted:
            item_reasons.append(
                f"source {source_id} terminal final_type mismatch"
            )
        if not delivery_ids_valid or not delivery_ids:
            item_reasons.append(
                f"source {source_id} terminal delivery_entity_ids are empty or invalid"
            )
        if not _has_attempt_evidence(terminal):
            item_reasons.append(f"source {source_id} terminal evidence is empty")
        for field in (
            "record_verified",
            "type_verified",
            "visual_verified",
            "ownership_verified",
        ):
            if terminal.get(field) is not True:
                item_reasons.append(
                    f"source {source_id} terminal {field} is not true"
                )
        removed_ids = set(_string_ids(terminal.get("removed_entity_ids"))[0])
        created_ids = set(_string_ids(terminal.get("created_entity_ids"))[0])
        reused_ids = set(_string_ids(terminal.get("reused_entity_ids"))[0])
        delivery_id_set = set(delivery_ids)
        support_ids = set(_string_ids(terminal.get("support_entity_ids"))[0])
        retained_ids = delivery_id_set.union(support_ids)
        if removed_ids.intersection(retained_ids):
            item_reasons.append(
                f"source {source_id} terminal removed entities remain delivered"
            )
        if not removed_ids.issubset(created_ids):
            item_reasons.append(
                f"source {source_id} terminal removed entities were not created"
            )
        if delivery_id_set.intersection(support_ids):
            item_reasons.append(
                f"source {source_id} terminal delivery and support entity roles overlap"
            )
        unowned_retained_ids = retained_ids.difference(created_ids)
        if reused_ids != unowned_retained_ids or reused_ids.intersection(
            created_ids.union(removed_ids)
        ):
            item_reasons.append(
                f"source {source_id} terminal reused_entity_ids do not bind unowned retained entities"
            )
        expected_created_ids = removed_ids.union(retained_ids.difference(reused_ids))
        if created_ids != expected_created_ids:
            item_reasons.append(
                f"source {source_id} terminal created entities are not exactly removed or retained-owned entities"
            )

        item_verified = not item_reasons
        for reason in item_reasons:
            _append_reason(reasons, reason)
        if item_verified:
            delivered_count += 1
        items.append(
            {
                "source_item_id": source_id,
                "terminal_attempt_index": terminal_index,
                "final_type": terminal_final or None,
                "verified": item_verified,
            }
        )

    lifecycle_reasons: list[str] = []
    created_occurrences: dict[str, list[int]] = {}
    all_created_or_removed: set[str] = set()
    all_reused: set[str] = set()
    for attempt_index, attempt in enumerate(attempts):
        if not isinstance(attempt, Mapping):
            continue
        created_ids, created_valid = _string_ids(attempt.get("created_entity_ids"))
        removed_ids, removed_valid = _string_ids(attempt.get("removed_entity_ids"))
        reused_ids, reused_valid = _string_ids(attempt.get("reused_entity_ids"))
        if created_valid:
            for entity_id in created_ids:
                created_occurrences.setdefault(entity_id, []).append(attempt_index)
            all_created_or_removed.update(created_ids)
        if removed_valid:
            all_created_or_removed.update(removed_ids)
        if reused_valid:
            all_reused.update(reused_ids)
    if any(len(indexes) > 1 for indexes in created_occurrences.values()):
        lifecycle_reasons.append(
            "created entity identities are not unique across attempts"
        )
    if all_reused.intersection(all_created_or_removed):
        lifecycle_reasons.append(
            "reused entities were created or removed elsewhere in the ledger"
        )

    terminal_delivery_ids = [
        entity_id
        for terminal in terminals
        for entity_id in _string_ids(terminal.get("delivery_entity_ids"))[0]
    ]
    if len(terminal_delivery_ids) != len(set(terminal_delivery_ids)):
        lifecycle_reasons.append(
            "terminal delivery_entity_ids are not unique across source items"
        )
    terminal_retained_ids = [
        entity_id
        for terminal in terminals
        for field in ("delivery_entity_ids", "support_entity_ids")
        for entity_id in _string_ids(terminal.get(field))[0]
    ]
    if len(terminal_retained_ids) != len(set(terminal_retained_ids)):
        lifecycle_reasons.append(
            "terminal retained entity identities are not unique across source items"
        )
    if lifecycle_reasons:
        for reason in lifecycle_reasons:
            _append_reason(reasons, reason)
        for item in items:
            item["verified"] = False
        delivered_count = 0

    source_count = len(items)
    if required and source_count == 0:
        _append_reason(reasons, "required delivery has no source items")
    delivery_verified = bool(
        not reasons
        and (not required or source_count > 0)
        and delivered_count == source_count
    )
    projection = {
        "schema": SCHEMA,
        "required": bool(required),
        "requested_type": requested,
        "verified": delivery_verified,
        "attempt_count": len(attempts),
        "source_item_count": source_count,
        "delivered_item_count": delivered_count,
        "failed_item_count": source_count - delivered_count,
        "items": items,
        "invalid_reasons": reasons,
    }
    return projection, terminals


def build_text_representation_delivery(
    attempts: Sequence[Mapping[str, Any]],
    *,
    requested_type: str,
    required: bool = True,
    expected_source_item_ids: Iterable[str] | None = None,
    fallback_ladders: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the compact schema-1.1 projection from one canonical ledger."""

    projection, _ = _project(
        attempts,
        requested_type=requested_type,
        required=required,
        expected_source_item_ids=expected_source_item_ids,
        fallback_ladders=fallback_ladders,
    )
    return projection


def resolve_text_representation_delivery(
    attempts: Sequence[Mapping[str, Any]],
    delivery: Mapping[str, Any],
    *,
    expected_source_item_ids: Iterable[str] | None = None,
    fallback_ladders: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute, compare, and resolve terminal attempts fail-closed.

    Consumers must use this resolver rather than trusting stored counts,
    indexes, or the stored ``verified`` flag.
    """

    if type(delivery) is not dict:
        return {
            "contract_valid": False,
            "delivery_verified": False,
            "verified": False,
            "items": [],
            "terminal_attempts": [],
            "invalid_reasons": ["text_representation_delivery is not an object"],
        }

    projection, terminals = _project(
        attempts,
        requested_type=delivery.get("requested_type"),
        required=delivery.get("required") is True,
        expected_source_item_ids=expected_source_item_ids,
        fallback_ladders=fallback_ladders,
    )
    comparison_reasons: list[str] = []
    if set(delivery) != set(DELIVERY_FIELDS):
        comparison_reasons.append(
            "text_representation_delivery keys do not exactly match schema"
        )
    for field in DELIVERY_FIELDS:
        if not _exact_json_equal(delivery.get(field), projection[field]):
            comparison_reasons.append(f"stored {field} does not match ledger projection")

    invalid_reasons = list(projection["invalid_reasons"])
    invalid_reasons.extend(comparison_reasons)
    contract_valid = not comparison_reasons
    delivery_verified = bool(projection["verified"])
    return {
        "contract_valid": contract_valid,
        "delivery_verified": delivery_verified,
        "verified": bool(contract_valid and delivery_verified),
        "items": projection["items"],
        "terminal_attempts": terminals,
        "invalid_reasons": invalid_reasons,
    }


__all__ = [
    "SCHEMA",
    "DELIVERY_FIELDS",
    "CANONICAL_FALLBACK_LADDERS",
    "build_text_representation_delivery",
    "resolve_text_representation_delivery",
]
