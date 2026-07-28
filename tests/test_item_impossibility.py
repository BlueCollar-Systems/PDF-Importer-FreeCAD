from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "PDFVectorImporter"))


def _module():
    name = "pdfcadcore.item_impossibility"
    spec = importlib.util.find_spec(name)
    assert spec is not None, "shared item-impossibility seal module is missing"
    return importlib.import_module(name)


def _kwargs() -> dict:
    return {
        "branch_evidence": {
            "renderer": "LibreCAD → DXF",
            "nested": {"verified": True, "count": 1},
        },
        "source_item_id": "p1:item:µ",
        "requested_type": "labels",
        "attempted_type": "labels",
        "strategy": "native_label",
        "reason": "native_label_unavailable",
        "host_outcome": "impossible",
        "cleanup_complete": True,
        "created_entity_ids": ["temporary:1", "temporary:2"],
        "removed_entity_ids": ["temporary:2", "temporary:1"],
        "delivery_entity_ids": [],
        "support_entity_ids": [],
        "referenced_entity_ids": ["source:1"],
        "reused_entity_ids": [],
        "owned_block_names": [],
    }


def test_builder_emits_exact_versioned_seal_and_verifier_accepts_it() -> None:
    module = _module()
    kwargs = _kwargs()
    proof = module.build_item_representation_impossibility_proof(**kwargs)

    assert proof["schema"] == "bcs.item_representation_impossibility/1.0"
    assert set(proof) == set(module.PROOF_FIELDS)
    assert module.item_representation_impossibility_proof_verified(
        proof, **kwargs
    )
    # The seal must be strict JSON, not merely something Python can compare.
    json.dumps(proof, allow_nan=False)

    sorted_round_trip = json.loads(json.dumps(proof, sort_keys=True))
    assert module.item_representation_impossibility_proof_verified(
        sorted_round_trip, **kwargs
    )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_builder_rejects_nonfinite_branch_evidence(value: float) -> None:
    module = _module()
    kwargs = _kwargs()
    kwargs["branch_evidence"] = {"measurement": value}
    with pytest.raises(ValueError):
        module.build_item_representation_impossibility_proof(**kwargs)


def test_branch_mutation_and_bool_int_ambiguity_fail_closed() -> None:
    module = _module()
    kwargs = _kwargs()
    kwargs["branch_evidence"] = {"value": True}
    proof = module.build_item_representation_impossibility_proof(**kwargs)

    mutated = dict(kwargs)
    mutated["branch_evidence"] = {"value": 1}
    assert not module.item_representation_impossibility_proof_verified(
        proof, **mutated
    )


def test_identity_list_permutations_produce_the_same_canonical_seal() -> None:
    module = _module()
    kwargs = _kwargs()
    kwargs["owned_block_names"] = ["block:b", "block:a"]
    proof = module.build_item_representation_impossibility_proof(**kwargs)
    permuted = dict(kwargs)
    for field_name in (
        "created_entity_ids",
        "removed_entity_ids",
        "referenced_entity_ids",
        "owned_block_names",
    ):
        permuted[field_name] = list(reversed(kwargs[field_name]))
    permuted_proof = module.build_item_representation_impossibility_proof(
        **permuted
    )

    assert permuted_proof == proof
    assert module.item_representation_impossibility_proof_verified(
        proof, **permuted
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("created_entity_ids", ["duplicate", "duplicate"]),
        ("referenced_entity_ids", ["temporary:1"]),
        ("delivery_entity_ids", ["retained"]),
        ("support_entity_ids", ["retained"]),
        ("reused_entity_ids", ["retained"]),
    ],
)
def test_builder_rejects_invalid_lifecycle_partitions(field: str, value: list) -> None:
    module = _module()
    kwargs = _kwargs()
    kwargs[field] = value
    with pytest.raises(ValueError):
        module.build_item_representation_impossibility_proof(**kwargs)


@pytest.mark.parametrize("mutation", ["inject", "remove", "tamper_digest"])
def test_proof_shape_and_digest_tampering_fail_closed(mutation: str) -> None:
    module = _module()
    kwargs = _kwargs()
    proof = module.build_item_representation_impossibility_proof(**kwargs)
    tampered = copy.deepcopy(proof)
    if mutation == "inject":
        tampered["forged"] = True
    elif mutation == "remove":
        tampered.pop("strategy")
    else:
        tampered["proof_sha256"] = "0" * 64
    assert not module.item_representation_impossibility_proof_verified(
        tampered, **kwargs
    )


def test_builder_rejects_reserved_seal_key_in_branch_evidence() -> None:
    module = _module()
    kwargs = _kwargs()
    kwargs["branch_evidence"][module.EVIDENCE_KEY] = {}
    with pytest.raises(ValueError):
        module.build_item_representation_impossibility_proof(**kwargs)


def test_verifier_is_total_for_cyclic_branch_evidence() -> None:
    module = _module()
    valid_kwargs = _kwargs()
    proof = module.build_item_representation_impossibility_proof(**valid_kwargs)
    cyclic: dict = {}
    cyclic["self"] = cyclic
    hostile_kwargs = dict(valid_kwargs)
    hostile_kwargs["branch_evidence"] = cyclic
    assert not module.item_representation_impossibility_proof_verified(
        proof, **hostile_kwargs
    )


def test_builder_rejects_cyclic_branch_evidence() -> None:
    module = _module()
    kwargs = _kwargs()
    cyclic: dict = {}
    cyclic["self"] = cyclic
    kwargs["branch_evidence"] = cyclic

    with pytest.raises(ValueError, match="cycle"):
        module.build_item_representation_impossibility_proof(**kwargs)
