"""Fictional JSON-only contract pilot; no PDF reader, producer or writer.

This unittest file supplies test-only consistency checks for the draft seed.
It is not a production parser, naming/fabrication rule or permission mechanism.
Run directly with Python -I -B; pytest may also collect the unittest class.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch
import urllib.request

from jsonschema import Draft202012Validator
from jsonschema.validators import extend

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = Path(os.environ.get(
    "BCS_DRAWING_CONTRACT_SCHEMA",
    str(ROOT / "docs/contracts/drawing-page-contract-v1.schema.json"),
))
FIXTURE_DIR = Path(os.environ.get(
    "BCS_DRAWING_CONTRACT_FIXTURES",
    str(ROOT / "tests/fixtures/drawing-page-contract-v1"),
))
VERSION = "bcs.drawing_page_contract/1-draft"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_SAFE_INTEGER = 9007199254740991

# Golden outcomes are authored here independently of fixture wrapper values.
GOLDEN = {
    "P01_exact_case_job_and_mixed_kind": (True, True, "CASE_DISTINCT_MAIN_MARKS LOSSLESS_JOB_IDENTITIES_DISTINCT"),
    "P02_revision_missing_empty_zero": (True, True, "REVISION_MISSING REVISION_EMPTY_LITERAL REVISION_ZERO_LITERAL"),
    "P03_read_quality_states_distinct": (True, True, "READABLE_UNRECOGNIZED NO_EXTRACTABLE_TEXT READ_ERROR UNSUPPORTED NOT_EVALUATED COVERAGE_PARTIAL"),
    "P04_drawing_number_and_bom_are_not_main_mark": (True, True, "MAIN_MARK_MISSING"),
    "P05_duplicate_bytes_separate_occurrences": (True, True, "DUPLICATE_BYTES_MULTIPLE_OCCURRENCES"),
    "P06_review_bound_to_current_interpretation": (True, True, "EXPLICIT_MAIN_MARK_PRESENT"),
    "P07_ambiguous_exact_case_alternatives": (True, True, "MAIN_MARK_AMBIGUOUS"),
    "N01_page_index_not_one_based": (False, False, "SCHEMA_INVALID PAGE_INDEX_NOT_ONE_BASED"),
    "N02_casefold_and_lossy_job_identity": (True, False, "LITERAL_EVIDENCE_MISMATCH LOSSLESS_IDENTIFIER_CHANGED"),
    "N03_dwg_or_bom_promotion_is_not_main_evidence": (True, False, "FIELD_ROLE_EVIDENCE_MISMATCH MAIN_MARK_NOT_ESTABLISHED"),
    "N04_numeric_revision_zero_not_literal": (False, False, "SCHEMA_INVALID LITERAL_MUST_BE_STRING"),
    "N05_missing_is_not_empty_literal": (False, False, "SCHEMA_INVALID FIELD_STATE_VALUE_CONTRADICTION"),
    "N06_stale_interpretation": (True, False, "INTERPRETATION_CONTENT_MISMATCH PROPOSAL_READY_STALE"),
    "N07_partial_claims_complete": (True, False, "COVERAGE_COMPLETE_CONTRADICTED PROPOSAL_READY_PARTIAL"),
    "N08_stale_rule_and_human_review_binding": (True, False, "RULE_INTERPRETATION_MISMATCH HUMAN_REVIEW_INTERPRETATION_MISMATCH"),
    "N09_ambiguous_requires_distinct_literals": (False, False, "SCHEMA_INVALID AMBIGUOUS_VALUES_NOT_DISTINCT"),
}

GOLDEN.update({
    "P08_known_letter_kind": (True, True, "MAIN_MARK_UNKNOWN"),
    "P09_known_printed_tag_sheet_no_text": (True, True, "NO_EXTRACTABLE_TEXT"),
    "N10_present_without_support": (False, False, "SCHEMA_INVALID PRESENT_EVIDENCE_REQUIRED"),
    "N11_ambiguous_without_support": (False, False, "SCHEMA_INVALID AMBIGUOUS_EVIDENCE_REQUIRED"),
    "N12_ambiguous_alternative_unsupported": (True, False, "AMBIGUOUS_ALTERNATIVE_UNSUPPORTED"),
    "N13_ambiguous_alternative_wrong_role": (True, False, "FIELD_ROLE_EVIDENCE_MISMATCH AMBIGUOUS_ALTERNATIVE_UNSUPPORTED"),
    "N14_unknown_root_format_version": (False, False, "SCHEMA_INVALID UNKNOWN_FORMAT_VERSION"),
    "N15_unknown_interpretation_contract_version": (False, False, "SCHEMA_INVALID UNKNOWN_INTERPRETATION_CONTRACT_VERSION"),
    "N16_unknown_rule_decision_version": (False, False, "SCHEMA_INVALID UNKNOWN_RULE_DECISION_VERSION"),
})
PAIR_GOLDEN = {
    "I00_unchanged_immutable_identity": (True, False, True, True, "INTERPRETATION_IDENTITY_UNCHANGED"),
    "I01_same_id_changed_reader": (True, True, False, False, "IMMUTABLE_INTERPRETATION_ID_CONFLICT"),
    "I02_same_id_changed_build": (True, True, False, False, "IMMUTABLE_INTERPRETATION_ID_CONFLICT"),
    "I03_same_id_changed_options": (True, True, False, False, "IMMUTABLE_INTERPRETATION_ID_CONFLICT"),
    "I04_same_id_changed_output_schema": (True, True, False, False, "IMMUTABLE_INTERPRETATION_ID_CONFLICT"),
    "I05_same_id_changed_content": (True, True, False, False, "IMMUTABLE_INTERPRETATION_ID_CONFLICT"),
    "I06_same_id_changed_page_facts": (True, True, False, False, "IMMUTABLE_INTERPRETATION_ID_CONFLICT"),
    "I07_new_id_reader_replacement_without_old_review": (True, False, False, True, "NEW_INTERPRETATION_ID OLD_HUMAN_REVIEW_NOT_APPLICABLE"),
    "I08_new_id_cannot_carry_old_human_review": (False, False, False, False, "HUMAN_REVIEW_INTERPRETATION_MISMATCH OLD_HUMAN_REVIEW_NOT_APPLICABLE"),
}
OP_GOLDEN = {
    "op-fiction-planned": (False, "OP_PLANNED_NOT_COMMITTED"),
    "op-fiction-committed": (True, "OP_COMMITTED_BOUND_PROOF_MATCH"),
    "op-fiction-interrupted": (False, "OP_INTERRUPTED_RECONCILIATION_REQUIRED"),
    "op-fiction-unresolved": (False, "OP_OUTCOME_UNRESOLVED"),
    "op-fiction-committed-size-mismatch": (False, "OP_EXPECTED_BYTE_LENGTH_MISMATCH"),
}

def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _nonfinite(_value):
    raise ValueError("NONFINITE_JSON_NUMBER")


def _finite_decimal(token):
    # Keep the existing finite binary-range rejection; never use its rounded
    # value as the parsed JSON number. Decimal(token) preserves all digits,
    # including finite underflow, without applying the arithmetic context.
    if not math.isfinite(float(token)):
        raise ValueError("NONFINITE_JSON_NUMBER")
    return Decimal(token)


def _json_text(text):
    return json.loads(text, object_pairs_hook=_unique_object,
                      parse_constant=_nonfinite, parse_float=_finite_decimal)


def _load(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_JSON_BYTES + 1)
    assert len(data) <= MAX_JSON_BYTES, "JSON_INPUT_TOO_LARGE"
    return _json_text(data.decode("utf-8"))


def _local_schema(schema):
    """Allow only resolved local $defs pointers before validator construction."""
    def visit(node, root=False):
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            if not root and ("$id" in node or "$schema" in node):
                raise ValueError("NESTED_SCHEMA_ID_OR_DIALECT")
            if "$dynamicRef" in node or "$recursiveRef" in node:
                raise ValueError("DYNAMIC_REFERENCE_NOT_IN_PILOT")
            if "$ref" in node:
                reference = node["$ref"]
                if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
                    raise ValueError("NONLOCAL_REFERENCE")
                target = schema
                try:
                    for token in reference[2:].split("/"):
                        if "~" in token.replace("~1", "").replace("~0", ""):
                            raise KeyError("invalid pointer escape")
                        target = target[token.replace("~1", "/").replace("~0", "~")]
                except (KeyError, TypeError):
                    raise ValueError("UNRESOLVED_LOCAL_REFERENCE") from None
                if not isinstance(target, (dict, bool)):
                    raise ValueError("REFERENCE_TARGET_NOT_SCHEMA")
            for value in node.values():
                visit(value)
    assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
    visit(schema, root=True)
    return schema


def _is_integral_number(value):
    """JSON mathematical integer, exact and independent of Decimal context."""
    if type(value) is int:
        return True
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    _sign, digits, exponent = value.as_tuple()
    if exponent >= 0:
        return True
    # Inspect digits only; no rounding, division, normalize or quantize.
    fractional_digits = min(-exponent, len(digits))
    return not any(digits[-fractional_digits:])


def _bounded_integer(value, minimum=0):
    return (_is_integral_number(value)
            and minimum <= value <= MAX_SAFE_INTEGER)


def _audit(record, validator):
    """Test-only invariant errors and observed reason facts; no value inference."""
    errors, facts = set(), set()
    if list(validator.iter_errors(record)):
        errors.add("SCHEMA_INVALID")
    content = record["content"]
    interpretation = record["interpretation"]
    pages = record["pages"]
    occurrences = record["occurrences"]
    if (not _bounded_integer(content["byte_length"], 0)
            or not _bounded_integer(content["page_count"], 1)):
        errors.add("INTEGER_METADATA_OUT_OF_RANGE")
    if record["format_version"] != VERSION:
        errors.add("UNKNOWN_FORMAT_VERSION")
    if interpretation["contract_version"] != VERSION:
        errors.add("UNKNOWN_INTERPRETATION_CONTRACT_VERSION")
    occurrence_ids = [item["occurrence_id"] for item in occurrences]
    if len(occurrence_ids) != len(set(occurrence_ids)):
        errors.add("DUPLICATE_OCCURRENCE_ID")
    if any(item["content_sha256"] != content["sha256"] for item in occurrences):
        errors.add("OCCURRENCE_CONTENT_MISMATCH")
    if len(occurrences) > 1:
        facts.add("DUPLICATE_BYTES_MULTIPLE_OCCURRENCES")
    stale = interpretation["source_content_sha256"] != content["sha256"]
    if stale:
        errors.add("INTERPRETATION_CONTENT_MISMATCH")
    indices = [page["page_index"] for page in pages]
    if any(not _is_integral_number(index) or index < 1 for index in indices):
        errors.add("PAGE_INDEX_NOT_ONE_BASED")
    if any(_is_integral_number(index)
           and (index > MAX_SAFE_INTEGER or index > content["page_count"])
           for index in indices):
        errors.add("PAGE_INDEX_OUT_OF_RANGE")
    if len(indices) != len(set(indices)):
        errors.add("DUPLICATE_PAGE_INDEX")
    complete = (_bounded_integer(content["page_count"], 1)
                and len(indices) == content["page_count"]
                and len(indices) == len(set(indices))
                and all(_bounded_integer(index, 1) and index <= content["page_count"]
                        for index in indices)
                and all(page["read_quality"] != "not_evaluated"
                        and page["kinds"]["state"] != "not_evaluated"
                        and all(field["state"] != "not_evaluated"
                                for field in page["fields"].values())
                        for page in pages))
    if interpretation["coverage"] == "complete" and not complete:
        errors.add("COVERAGE_COMPLETE_CONTRADICTED")
    if interpretation["coverage"] == "partial":
        facts.add("COVERAGE_PARTIAL")
    marks, jobs = set(), set()
    for page in pages:
        quality = page["read_quality"]
        if quality == "text_available" and page["kinds"]["state"] == "unknown":
            facts.add("READABLE_UNRECOGNIZED")
        quality_codes = {"no_extractable_text": "NO_EXTRACTABLE_TEXT",
                         "read_error": "READ_ERROR", "unsupported": "UNSUPPORTED",
                         "not_evaluated": "NOT_EVALUATED"}
        if quality in quality_codes:
            facts.add(quality_codes[quality])
        for role, field in page["fields"].items():
            state, evidence = field["state"], field["evidence"]
            if any(item["role"] != role for item in evidence):
                errors.add("FIELD_ROLE_EVIDENCE_MISMATCH")
            if state in ("missing", "not_evaluated") and evidence:
                errors.add("FIELD_STATE_EVIDENCE_FORBIDDEN")
            if state == "present" and not evidence:
                errors.add("PRESENT_EVIDENCE_REQUIRED")
            if state == "ambiguous":
                if len(evidence) < 2:
                    errors.add("AMBIGUOUS_EVIDENCE_REQUIRED")
                alternatives = field.get("alternatives", [])
                if any(item["role"] != role for item in evidence):
                    errors.add("FIELD_ROLE_EVIDENCE_MISMATCH")
                for literal in alternatives:
                    if not any(item["role"] == role and item["literal"] == literal
                               for item in evidence):
                        errors.add("AMBIGUOUS_ALTERNATIVE_UNSUPPORTED")
                if any(item["literal"] not in alternatives for item in evidence):
                    errors.add("LITERAL_EVIDENCE_MISMATCH")
            if role == "main_mark" and state == "unknown":
                facts.add("MAIN_MARK_UNKNOWN")
            if (state == "present") != ("literal" in field):
                errors.add("FIELD_STATE_VALUE_CONTRADICTION")
            if "literal" in field and not isinstance(field["literal"], str):
                errors.add("LITERAL_MUST_BE_STRING")
            if state == "ambiguous":
                values = field.get("alternatives", [])
                if len(values) < 2 or len(values) != len(set(values)):
                    errors.add("AMBIGUOUS_VALUES_NOT_DISTINCT")
                if role == "main_mark":
                    facts.add("MAIN_MARK_AMBIGUOUS")
            if state == "present":
                literal = field.get("literal")
                if any(item["role"] != role for item in evidence):
                    errors.add("FIELD_ROLE_EVIDENCE_MISMATCH")
                if any(item["literal"] != literal for item in evidence):
                    errors.add("LITERAL_EVIDENCE_MISMATCH")
                    if role in ("job_number", "drawing_number", "main_mark"):
                        errors.add("LOSSLESS_IDENTIFIER_CHANGED")
                if role == "main_mark":
                    supported = any(item["role"] == role
                                    and item["context"] != "bom"
                                    and item["literal"] == literal for item in evidence)
                    if not supported:
                        errors.add("MAIN_MARK_NOT_ESTABLISHED")
                    # This fixture fact is narrower than general field support;
                    # human/inferred evidence is not relabelled as an explicit label.
                    if any(item["role"] == role and item["method"] == "explicit_label"
                           and item["context"] in ("title_block", "member_callout")
                           and item["literal"] == literal for item in evidence):
                        facts.add("EXPLICIT_MAIN_MARK_PRESENT")
                    if isinstance(literal, str):
                        marks.add(literal)
                if role == "job_number" and isinstance(literal, str):
                    jobs.add(literal)
            if role == "main_mark" and state == "missing":
                facts.add("MAIN_MARK_MISSING")
            if role == "revision":
                if state == "missing":
                    facts.add("REVISION_MISSING")
                elif state == "present" and field.get("literal") == "":
                    facts.add("REVISION_EMPTY_LITERAL")
                elif state == "present" and field.get("literal") == "0":
                    facts.add("REVISION_ZERO_LITERAL")
    if len(marks) > len({mark.casefold() for mark in marks}):
        facts.add("CASE_DISTINCT_MAIN_MARKS")
    if len(jobs) > 1:
        facts.add("LOSSLESS_JOB_IDENTITIES_DISTINCT")
    for decision in record["rule_decisions"]:
        if decision["format_version"] != "bcs.drawing_rule_decision/1-draft":
            errors.add("UNKNOWN_RULE_DECISION_VERSION")
        if decision["interpretation_id"] != interpretation["id"]:
            errors.add("RULE_INTERPRETATION_MISMATCH")
        if decision["outcome"] == "proposal_ready":
            if stale:
                errors.add("PROPOSAL_READY_STALE")
            if interpretation["coverage"] != "complete" or not complete:
                errors.add("PROPOSAL_READY_PARTIAL")
    for review in record["human_reviews"]:
        if review["interpretation_id"] != interpretation["id"]:
            errors.add("HUMAN_REVIEW_INTERPRETATION_MISMATCH")
    return errors, facts


def _same_json_value(left, right):
    """Test-only JSON value equality, not canonical serialization or hashing."""
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is bool and type(right) is bool and left == right
    left_number = type(left) is int or isinstance(left, Decimal)
    right_number = type(right) is int or isinstance(right, Decimal)
    if left_number or right_number:
        if not (left_number and right_number):
            return False
        if any(isinstance(value, Decimal) and not value.is_finite()
               for value in (left, right)):
            raise ValueError("NONFINITE_JSON_NUMBER")
        # Decimal/int equality is exact and does not round through the current
        # Decimal arithmetic context. Equal numeric spellings compare equal.
        return left == right
    if isinstance(left, str) or isinstance(right, str):
        return type(left) is str and type(right) is str and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (isinstance(left, list) and isinstance(right, list)
                and len(left) == len(right)
                and all(_same_json_value(a, b) for a, b in zip(left, right)))
    if isinstance(left, dict) or isinstance(right, dict):
        return (isinstance(left, dict) and isinstance(right, dict)
                and left.keys() == right.keys()
                and all(_same_json_value(left[key], right[key]) for key in left))
    raise TypeError("NON_JSON_VALUE")


def _same_interpretation_id_conflicts(old, new):
    """Immutable payload equality only; not an ID/hash minting algorithm."""
    def binding(record):
        item = {key: value for key, value in record["interpretation"].items()
                if key != "id"}
        return {"content": record["content"], "interpretation": item,
                "pages": record["pages"]}
    return (old["interpretation"]["id"] == new["interpretation"]["id"]
            and not _same_json_value(binding(old), binding(new)))


def _materialize(baseline, replacements):
    """Replace-only fictional in-memory JSON; no filesystem operation."""
    result = deepcopy(baseline)
    for change in replacements:
        path = change["path"]
        if not isinstance(path, list) or not path:
            raise ValueError("INVALID_REPLACEMENT_PATH")
        parent = result
        for key in path[:-1]:
            if isinstance(parent, list):
                if type(key) is not int or not 0 <= key < len(parent):
                    raise ValueError("INVALID_REPLACEMENT_INDEX")
            elif not isinstance(parent, dict) or key not in parent:
                raise ValueError("REPLACEMENT_TARGET_MISSING")
            parent = parent[key]
        key = path[-1]
        if isinstance(parent, list):
            if type(key) is not int or not 0 <= key < len(parent):
                raise ValueError("INVALID_REPLACEMENT_INDEX")
        elif not isinstance(parent, dict) or key not in parent:
            raise ValueError("REPLACEMENT_TARGET_MISSING")
        parent[key] = deepcopy(change["value"])
    return result


def _operation_claim(record, content_record):
    """Evaluate fictional claim metadata only, never actual file proof."""
    content = content_record["content"]
    if (record["bound_content_sha256"] != content["sha256"]
            or not _bounded_integer(record["expected_byte_length"], 0)
            or record["expected_byte_length"] != content["byte_length"]
            or record["bound_interpretation_id"] != content_record["interpretation"]["id"]):
        return False, {"OP_BOUND_CONTENT_MISMATCH"}
    state = record["state"]
    noncommitted = {"planned": "OP_PLANNED_NOT_COMMITTED",
                    "interrupted": "OP_INTERRUPTED_RECONCILIATION_REQUIRED",
                    "unresolved": "OP_OUTCOME_UNRESOLVED"}
    if state in noncommitted:
        return False, {noncommitted[state]}
    proof = record["verification"]
    if (state != "committed" or proof.get("state") != "verified"
            or proof.get("observed_content_sha256") != content["sha256"]
            or proof.get("observed_after_locator") != record["intended_after_locator"]):
        return False, {"OP_COMMITTED_PROOF_MISMATCH"}
    if (not _bounded_integer(proof.get("observed_byte_length"), 0)
            or proof["observed_byte_length"] != record["expected_byte_length"]):
        return False, {"OP_EXPECTED_BYTE_LENGTH_MISMATCH"}
    return True, {"OP_COMMITTED_BOUND_PROOF_MATCH"}


class DrawingPageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.offline = ExitStack()
        cls.addClassCleanup(cls.offline.close)
        def blocked(*_args, **_kwargs):
            raise AssertionError("NETWORK_ACCESS_NOT_ALLOWED")
        for obj, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                          (socket, "create_connection"), (urllib.request, "urlopen")):
            cls.offline.enter_context(patch.object(obj, name, blocked))
        cls.input_paths = (SCHEMA_PATH,) + tuple(FIXTURE_DIR / name for name in (
            "fixtures-positive.json", "fixtures-negative.json",
            "fixtures-interpretation-pairs.json", "optional-operation-examples.json"))
        before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in cls.input_paths]
        def unchanged():
            after = [hashlib.sha256(path.read_bytes()).hexdigest() for path in cls.input_paths]
            if before != after:
                raise AssertionError("JSON_INPUT_CHANGED")
        cls.addClassCleanup(unchanged)
        cls.schema = _local_schema(_load(SCHEMA_PATH))
        Draft202012Validator.check_schema(cls.schema)
        integer_checker = Draft202012Validator.TYPE_CHECKER.redefine(
            "integer", lambda _checker, value: _is_integral_number(value))
        cls.validator = extend(Draft202012Validator, type_checker=integer_checker)(cls.schema)
        cls.cases = {}
        for filename, count, prefix in (("fixtures-positive.json", 9, "P"),
                                        ("fixtures-negative.json", 16, "N")):
            packet = _load(FIXTURE_DIR / filename)
            if len(packet["cases"]) != count:
                raise AssertionError("FIXTURE_COUNT_CHANGED")
            for case in packet["cases"]:
                key = case["fixture_id"]
                if (key in cls.cases or not key.startswith(prefix)
                        or case["wholly_fictional"] is not True
                        or case["all_sha256_values_are_dummy_not_content_hashes"] is not True):
                    raise AssertionError("FIXTURE_ID_OR_PROVENANCE")
                cls.cases[key] = case
        if set(cls.cases) != set(GOLDEN):
            raise AssertionError("FIXTURE_ROSTER_CHANGED")
        cls.pairs_packet = _load(FIXTURE_DIR / "fixtures-interpretation-pairs.json")
        if (cls.pairs_packet["wholly_fictional"] is not True
                or cls.pairs_packet["all_sha256_values_are_dummy_not_content_hashes"] is not True):
            raise AssertionError("PAIR_PROVENANCE")
        cls.pairs = {case["fixture_id"]: case for case in cls.pairs_packet["cases"]}
        if len(cls.pairs) != 9 or len(cls.pairs_packet["cases"]) != 9 or set(cls.pairs) != set(PAIR_GOLDEN):
            raise AssertionError("PAIR_ROSTER_CHANGED")
        packet = _load(FIXTURE_DIR / "optional-operation-examples.json")
        cls.operations = {case["record"]["operation_id"]: case for case in packet["cases"]}
        if len(cls.operations) != 5 or len(packet["cases"]) != 5 or set(cls.operations) != set(OP_GOLDEN):
            raise AssertionError("OPERATION_ROSTER_CHANGED")

    def positive(self, key="P06_review_bound_to_current_interpretation"):
        return deepcopy(self.cases[key]["record"])

    def test_case_lossless_and_mixed_kind(self):
        record = self.positive("P01_exact_case_job_and_mixed_kind")
        self.assertEqual([p["fields"]["main_mark"]["literal"] for p in record["pages"]], ["M7", "m7"])
        self.assertEqual([p["fields"]["job_number"]["literal"] for p in record["pages"]], ["AB-0042-a", "AB-0042-A"])
        self.assertEqual(record["pages"][0]["kinds"]["values"], ["drawing", "bill_of_materials"])
        self.assertEqual(record["rule_decisions"][0]["outcome"], "needs_review")

    def test_revision_and_read_states_do_not_collapse(self):
        pages = self.positive("P02_revision_missing_empty_zero")["pages"]
        self.assertNotIn("literal", pages[0]["fields"]["revision"])
        self.assertEqual([p["fields"]["revision"].get("literal") for p in pages], [None, "", "0"])
        pages = self.positive("P03_read_quality_states_distinct")["pages"]
        self.assertEqual([p["read_quality"] for p in pages], ["text_available", "no_extractable_text", "read_error", "unsupported", "not_evaluated"])
        self.assertEqual(pages[1]["fields"]["main_mark"]["state"], "unknown")
        self.assertEqual(pages[2]["fields"]["main_mark"]["state"], "not_evaluated")

    def test_known_letter_and_tag_kinds_do_not_imply_readability(self):
        letter = self.positive("P08_known_letter_kind")["pages"][0]
        tags = self.positive("P09_known_printed_tag_sheet_no_text")["pages"][0]
        self.assertEqual(letter["kinds"]["values"], ["letter"])
        self.assertEqual(tags["kinds"]["values"], ["printed_tag_sheet"])
        self.assertEqual(tags["read_quality"], "no_extractable_text")
        self.assertEqual(tags["fields"]["main_mark"]["state"], "unknown")

    def test_occurrences_remain_distinct_without_changing_interpretation(self):
        old = self.positive("P05_duplicate_bytes_separate_occurrences")
        self.assertEqual(len({item["occurrence_id"] for item in old["occurrences"]}), 2)
        self.assertEqual(len({item["content_sha256"] for item in old["occurrences"]}), 1)
        changed = deepcopy(old)
        changed["occurrences"][0]["locator"] = "fixture://invented/moved.pdf"
        self.assertFalse(_same_interpretation_id_conflicts(old, changed))

    def test_no_mutation_extension_or_auditor_write(self):
        record = self.positive()
        before = deepcopy(record)
        _audit(record, self.validator)
        self.assertEqual(record, before)
        for name in ("writer", "rename", "output_path", "fabrication_approved"):
            with self.subTest(case_id="writer-extension-" + name):
                changed = deepcopy(record)
                changed[name] = True
                self.assertTrue(list(self.validator.iter_errors(changed)))

    def test_duplicate_indices_occurrences_and_wrong_occurrence_digest(self):
        record = self.positive("P05_duplicate_bytes_separate_occurrences")
        record["pages"].append(deepcopy(record["pages"][0]))
        record["occurrences"][1]["occurrence_id"] = record["occurrences"][0]["occurrence_id"]
        record["occurrences"][1]["content_sha256"] = "d" * 64
        self.assertTrue({"DUPLICATE_PAGE_INDEX", "DUPLICATE_OCCURRENCE_ID", "OCCURRENCE_CONTENT_MISMATCH"} <= _audit(record, self.validator)[0])

    def test_metadata_type_and_size_boundaries(self):
        for field, value, case_id in (("byte_length", -1, "negative-size"), ("byte_length", True, "boolean-size"),
                                      ("page_count", 0, "zero-pages"), ("page_count", True, "boolean-pages")):
            with self.subTest(case_id=case_id):
                record = self.positive()
                record["content"][field] = value
                self.assertTrue(list(self.validator.iter_errors(record)))
        for value, code, case_id in ((0, "PAGE_INDEX_NOT_ONE_BASED", "page-zero"),
                                      (True, "PAGE_INDEX_NOT_ONE_BASED", "page-boolean"),
                                      (2, "PAGE_INDEX_OUT_OF_RANGE", "page-out-of-range")):
            with self.subTest(case_id=case_id):
                record = self.positive()
                record["pages"][0]["page_index"] = value
                self.assertIn(code, _audit(record, self.validator)[0])
        record = self.positive()
        record["content"]["page_count"] = 10 ** 20
        self.assertIn("COVERAGE_COMPLETE_CONTRADICTED", _audit(record, self.validator)[0])

    def test_exact_option_scalar_types_are_not_coalesced(self):
        old = self.positive()
        old["interpretation"]["options"] = {"value": False}
        for value, case_id in ((0, "false-vs-zero"), ("false", "false-vs-text")):
            with self.subTest(case_id=case_id):
                changed = deepcopy(old)
                changed["interpretation"]["options"]["value"] = value
                self.assertTrue(_same_interpretation_id_conflicts(old, changed))

    def test_lossless_decimal_values_and_spelling(self):
        # These independently authored controls exercise parsing and the actual
        # immutable-payload comparison, under deliberately low precision.
        controls = (
            ("0.1", "0.10000000000000001", False, "decimal-tenths-distinct"),
            ("1.0", "1.00000000000000001", False, "near-one-distinct"),
            ("1e100", "1.0000000000000000000000000000000000001e100", False, "large-exponent-distinct"),
            ("1e-400", "0", False, "finite-underflow-not-zero"),
            ("1e-400", "2e-400", False, "finite-underflows-distinct"),
            ("0.10", "0.1", True, "equal-decimal-spelling"),
            ("1e0", "1.000", True, "equal-integral-spelling"),
            ("-0.0", "0", True, "equal-signed-zero-value"),
            ("1e100", "10e99", True, "equal-exponent-spelling"),
            ("0.10000000000000001", "0.10000000000000001", True, "unchanged-exact-decimal"),
            ("0.1", '\"0.1\"', False, "number-not-string"),
            ("1.0", "true", False, "number-not-boolean"),
            ('{\"a\":0.1,\"b\":[1.0,true]}', '{\"b\":[1,true],\"a\":0.10}', True, "object-order-ignored"),
            ("[0.1,0.2]", "[0.2,0.1]", False, "list-order-preserved"),
            ("9007199254740992.0", "9007199254740993.0", False, "above-safe-integer-decimals-distinct"),
            ('{"\U00010000":0.1,"\uE000":[1,true]}', '{"\uE000":[1.0,true],"\U00010000":0.10}', True, "unicode-object-order-ignored"),
        )
        with localcontext() as context:
            context.prec = 2
            for left_json, right_json, equal, case_id in controls:
                with self.subTest(case_id=case_id):
                    left = self.positive()
                    right = self.positive()
                    left["interpretation"]["options"] = _json_text('{\"value\":' + left_json + '}')
                    right["interpretation"]["options"] = _json_text('{\"value\":' + right_json + '}')
                    self.assertFalse(list(self.validator.iter_errors(left)))
                    self.assertFalse(list(self.validator.iter_errors(right)))
                    self.assertIs(_same_interpretation_id_conflicts(left, right), not equal)

    def test_hash_slot_exact_width_and_alphabet(self):
        faults = (
            ("valid64", "a" * 64, True),
            ("lf", "a" * 64 + "\n", False),
            ("crlf", "a" * 64 + "\r\n", False),
            ("space", "a" * 63 + " ", False),
            ("short63", "a" * 63, False),
            ("long65", "a" * 65, False),
            ("nonhex", "g" + "a" * 63, False),
        )
        for group in ("content", "occurrence", "interpretation", "coherent"):
            for fault, value, valid in faults:
                with self.subTest(case_id=group + "-" + fault):
                    record = self.positive()
                    if group in ("content", "coherent"):
                        record["content"]["sha256"] = value
                    if group in ("occurrence", "coherent"):
                        record["occurrences"][0]["content_sha256"] = value
                    if group in ("interpretation", "coherent"):
                        record["interpretation"]["source_content_sha256"] = value
                    self.assertIs(not list(self.validator.iter_errors(record)), valid)
                    self.assertIs(not _audit(record, self.validator)[0], valid)
        # Conditional array-size keywords remain portable to strict validators.
        pending = [self.schema]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                if "minItems" in node or "maxItems" in node:
                    self.assertEqual(node.get("type"), "array")
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)

    def test_bounded_integer_metadata(self):
        fields = (("byte_length", ["content", "byte_length"], 0),
                  ("page_count", ["content", "page_count"], 1),
                  ("page_index", ["pages", 0, "page_index"], 1))
        for name, path, minimum in fields:
            values = (("zero", 0, minimum == 0), ("one", 1, True),
                      ("maximum", MAX_SAFE_INTEGER, True),
                      ("above-maximum", MAX_SAFE_INTEGER + 1, False),
                      ("negative", -1, False), ("fraction", Decimal("1.5"), False),
                      ("near-bound-fraction", _json_text("9007199254740990.1"), False),
                      ("boolean", True, False), ("integral-decimal", Decimal("1.0"), True))
            for label, value, valid in values:
                with self.subTest(case_id=name + "-" + label):
                    record = _materialize(self.positive(), [{"path": path, "value": value}])
                    self.assertIs(not list(self.validator.iter_errors(record)), valid)
                    self.assertIs(_bounded_integer(value, minimum), valid)

    def test_whole_valued_metadata_and_page_audit(self):
        record = _materialize(self.positive(), [
            {"path": ["content", "byte_length"], "value": Decimal("12000.0")},
            {"path": ["content", "page_count"], "value": Decimal("1.0")},
            {"path": ["pages", 0, "page_index"], "value": Decimal("1e0")},
        ])
        with localcontext() as context:
            context.prec = 1
            self.assertFalse(list(self.validator.iter_errors(record)))
            self.assertFalse(_audit(record, self.validator)[0])
            self.assertTrue(_bounded_integer(Decimal("9007199254740991.0")))
            self.assertFalse(_is_integral_number(Decimal("1.00000000000000001")))

    def test_evidence_state_role_rules(self):
        raw = deepcopy(self.positive()["pages"][0]["fields"]["main_mark"]["evidence"][0])
        for state in ("present", "ambiguous", "missing", "not_evaluated", "unknown", "unsupported"):
            record = self.positive()
            record["interpretation"]["coverage"] = "partial"
            record["rule_decisions"][0]["outcome"] = "needs_review"
            record["human_reviews"] = []
            if state == "present":
                field = deepcopy(record["pages"][0]["fields"]["main_mark"])
            elif state == "ambiguous":
                field = deepcopy(self.positive("P07_ambiguous_exact_case_alternatives")["pages"][0]["fields"]["main_mark"])
            else:
                field = {"state": state, "evidence": []}
                if state in ("unknown", "unsupported"):
                    observation = deepcopy(raw)
                    observation["literal"] = "EX?"
                    field["evidence"] = [observation]
            record["pages"][0]["fields"]["main_mark"] = field
            with self.subTest(case_id=state + "-valid"):
                self.assertFalse(list(self.validator.iter_errors(record)))
                errors, facts = _audit(record, self.validator)
                self.assertFalse(errors)
                if state in ("unknown", "unsupported"):
                    self.assertEqual(field["evidence"][0]["literal"], "EX?")
                    self.assertNotIn("literal", field)
                    self.assertNotIn("alternatives", field)
                    self.assertNotIn("EXPLICIT_MAIN_MARK_PRESENT", facts)
            with self.subTest(case_id=state + "-wrong-role"):
                changed = deepcopy(record)
                wrong = changed["pages"][0]["fields"]["main_mark"]
                if not wrong["evidence"]:
                    wrong["evidence"] = [deepcopy(raw)]
                wrong["evidence"][0]["role"] = "drawing_number"
                shape_valid = state not in ("missing", "not_evaluated")
                self.assertIs(not list(self.validator.iter_errors(changed)), shape_valid)
                self.assertIn("FIELD_ROLE_EVIDENCE_MISMATCH", _audit(changed, self.validator)[0])
            if state in ("missing", "not_evaluated"):
                with self.subTest(case_id=state + "-forbidden-observation"):
                    changed = deepcopy(record)
                    changed["pages"][0]["fields"]["main_mark"]["evidence"] = [deepcopy(raw)]
                    self.assertTrue(list(self.validator.iter_errors(changed)))
                    self.assertIn("FIELD_STATE_EVIDENCE_FORBIDDEN", _audit(changed, self.validator)[0])
        with self.subTest(case_id="unclear-is-not-v1-state"):
            record = self.positive()
            record["pages"][0]["fields"]["main_mark"] = {"state": "unclear", "evidence": []}
            self.assertTrue(list(self.validator.iter_errors(record)))

    def test_optional_integer_lengths(self):
        base = self.operations["op-fiction-committed"]["record"]
        content = self.positive()
        values = (("whole-valued", Decimal("12000.0"), True),
                  ("boolean", True, False), ("fraction", Decimal("12000.5"), False),
                  ("above-maximum", MAX_SAFE_INTEGER + 1, False))
        for field in ("expected", "observed"):
            for label, value, valid in values:
                with self.subTest(case_id=field + "-" + label):
                    operation = deepcopy(base)
                    if field == "expected":
                        operation["expected_byte_length"] = value
                    else:
                        operation["verification"]["observed_byte_length"] = value
                    self.assertIs(_operation_claim(operation, content)[0], valid)
        for label, value, valid in (("zero", 0, True), ("maximum", MAX_SAFE_INTEGER, True),
                                    ("above-maximum", MAX_SAFE_INTEGER + 1, False),
                                    ("boolean", True, False), ("fraction", Decimal("1.5"), False)):
            with self.subTest(case_id="coherent-" + label):
                operation = deepcopy(base)
                bound = deepcopy(content)
                bound["content"]["byte_length"] = value
                operation["expected_byte_length"] = value
                operation["verification"]["observed_byte_length"] = value
                self.assertIs(_operation_claim(operation, bound)[0], valid)

    def test_nonlocal_or_unresolved_schema_refs_are_rejected(self):
        for reference, case_id in (("https://invalid.example/schema", "https"),
                                   ("file:///not-read.json", "file"), ("../other.json", "relative"),
                                   ("#/$defs/not_present", "missing-local")):
            with self.subTest(case_id=case_id):
                changed = deepcopy(self.schema)
                changed["properties"]["interpretation"]["$ref"] = reference
                with self.assertRaises(ValueError):
                    _local_schema(changed)

    def test_nested_ids_dynamic_refs_and_unknown_dialect_rejected(self):
        for key, value, case_id in (("$id", "urn:fictional:other", "nested-id"),
                                    ("$dynamicRef", "#/$defs/page", "dynamic-ref")):
            with self.subTest(case_id=case_id):
                changed = deepcopy(self.schema)
                changed["$defs"]["page"][key] = value
                with self.assertRaises(ValueError):
                    _local_schema(changed)
        changed = deepcopy(self.schema)
        changed["$schema"] = "https://invalid.example/dialect"
        with self.assertRaises(AssertionError):
            _local_schema(changed)

    def test_network_denial_is_active(self):
        with self.assertRaisesRegex(AssertionError, "NETWORK_ACCESS_NOT_ALLOWED"):
            socket.create_connection(("invalid.example", 443))

    def test_ambiguous_or_nonfinite_json_rejected(self):
        for payload, case_id in (("{\"a\":1,\"a\":2}", "duplicate-key"),
                                  ("{\"value\":NaN}", "nan"), ("{\"value\":Infinity}", "infinity"),
                                  ("{\"value\":1e999}", "exponent-overflow")):
            with self.subTest(case_id=case_id):
                with self.assertRaises(ValueError):
                    _json_text(payload)


def _fixture_test(case_id):
    def test(self):
        case = self.cases[case_id]
        shape, valid, codes = GOLDEN[case_id]
        expected = case["expected"]
        self.assertIs(expected["schema_valid"], shape)
        self.assertIs(expected["semantic_contract_valid"], valid)
        self.assertEqual(set(expected["reason_codes"]), set(codes.split()))
        self.assertIs(not list(self.validator.iter_errors(case["record"])), shape)
        errors, facts = _audit(case["record"], self.validator)
        self.assertIs(not errors, valid, (case_id, sorted(errors)))
        self.assertTrue(set(codes.split()) <= (errors | facts), case_id)
    return test


def _pair_test(case_id):
    def test(self):
        case = self.pairs[case_id]
        right_valid, conflict, applicable, pair_valid, minimum = PAIR_GOLDEN[case_id]
        left = deepcopy(self.pairs_packet["baseline_record"])
        right = _materialize(left, case["right_replacements"])
        expected = case["expected"]
        for key in ("left_schema_valid", "right_schema_valid"):
            self.assertIs(expected[key], True)
        self.assertIs(expected["right_semantic_contract_valid"], right_valid)
        self.assertIs(expected["interpretation_id_conflict"], conflict)
        self.assertIs(expected["old_human_review_applicable"], applicable)
        self.assertIs(expected["pair_identity_valid"], pair_valid)
        self.assertEqual(set(expected["reason_codes"]), set(minimum.split()))
        self.assertFalse(list(self.validator.iter_errors(left)))
        self.assertFalse(list(self.validator.iter_errors(right)))
        self.assertFalse(_audit(left, self.validator)[0])
        errors, _facts = _audit(right, self.validator)
        self.assertIs(not errors, right_valid)
        actual_conflict = _same_interpretation_id_conflicts(left, right)
        actual_applicable = not actual_conflict and any(
            review["interpretation_id"] == right["interpretation"]["id"]
            and review["verdict"] == "confirmed" for review in left["human_reviews"])
        self.assertIs(actual_conflict, conflict)
        self.assertIs(actual_applicable, applicable)
        self.assertIs(not actual_conflict and not errors, pair_valid)
        codes = set(errors)
        if actual_conflict:
            codes.add("IMMUTABLE_INTERPRETATION_ID_CONFLICT")
        elif left["interpretation"]["id"] == right["interpretation"]["id"]:
            codes.add("INTERPRETATION_IDENTITY_UNCHANGED")
        else:
            codes.add("NEW_INTERPRETATION_ID")
            if not actual_applicable:
                codes.add("OLD_HUMAN_REVIEW_NOT_APPLICABLE")
        self.assertTrue(set(minimum.split()) <= codes)
    return test


def _operation_test(case_id):
    def test(self):
        case = self.operations[case_id]
        self.assertIs(case["wholly_fictional"], True)
        self.assertIs(case["all_sha256_values_are_dummy_not_content_hashes"], True)
        complete, minimum = OP_GOLDEN[case_id]
        self.assertIs(case["expected"]["is_complete"], complete)
        self.assertIs(case["expected"]["grants_mutation_authority"], False)
        self.assertEqual(set(case["expected"]["reason_codes"]), set(minimum.split()))
        record = case["record"]
        bound = self.cases[record["bound_content_fixture_id"]]["record"]
        actual, codes = _operation_claim(record, bound)
        self.assertIs(actual, complete)
        self.assertTrue(set(minimum.split()) <= codes)
        # The optional protocol never becomes part of the page schema.
        self.assertTrue(list(self.validator.iter_errors(record)))
    return test


for _case_id in GOLDEN:
    setattr(DrawingPageContractTests, "test_fixture_" + _case_id, _fixture_test(_case_id))
for _case_id in PAIR_GOLDEN:
    setattr(DrawingPageContractTests, "test_pair_" + _case_id, _pair_test(_case_id))
for _case_id in OP_GOLDEN:
    setattr(DrawingPageContractTests, "test_operation_" + _case_id.replace("-", "_"), _operation_test(_case_id))


if __name__ == "__main__":
    class RecordedResult(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.executed_case_ids = []
        def startTest(self, test):
            self.executed_case_ids.append(test.id().rsplit(".", 1)[-1])
            super().startTest(test)
        def addSubTest(self, test, subtest, err):
            case_id = subtest.params.get("case_id")
            if case_id:
                self.executed_case_ids.append(test.id().rsplit(".", 1)[-1] + ":" + case_id)
            super().addSubTest(test, subtest, err)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DrawingPageContractTests)
    declared_count = suite.countTestCases()
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1,
                                     resultclass=RecordedResult).run(suite)
    passed = (result.wasSuccessful() and not result.skipped
              and result.testsRun == declared_count and declared_count > 0)
    print(json.dumps({"status": "PASS" if passed else "FAIL",
                      "contract_version": VERSION, "declared_tests": declared_count,
                      "tests_run": result.testsRun, "failures": len(result.failures),
                      "errors": len(result.errors), "skips": len(result.skipped),
                      "executed_case_ids": result.executed_case_ids}, sort_keys=True))
    raise SystemExit(0 if passed else 1)
