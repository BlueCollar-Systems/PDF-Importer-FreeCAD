from __future__ import annotations

import math
import random

import pytest

from PDFVectorImporter.pdfcadcore import hatch_detector


def _line(angle: float, source_index: int, group_position: int = 0, base: float = 0.0):
    radians = math.radians(base)
    spacing = 2.0 * group_position
    return {
        "idx": 1000 + source_index,
        "pid": 2000 + source_index,
        "angle": angle,
        "len": 10.0,
        "mx": -math.sin(radians) * spacing,
        "my": math.cos(radians) * spacing,
        "source_index": source_index,
    }


def _literal_accept(group, id_key):
    if len(group) < 6:
        return set()
    radians = math.radians(group[0]["angle"])
    perpendicular = (-math.sin(radians), math.cos(radians))
    projections = sorted(
        line["mx"] * perpendicular[0] + line["my"] * perpendicular[1]
        for line in group
    )
    spacings = [
        abs(projections[index] - projections[index - 1])
        for index in range(1, len(projections))
    ]
    mean_spacing = sum(spacings) / len(spacings)
    if mean_spacing < 0.3:
        return set()
    spacing_variance = sum(
        (spacing - mean_spacing) ** 2 for spacing in spacings
    ) / len(spacings)
    if math.sqrt(spacing_variance) / mean_spacing >= 0.35:
        return set()
    lengths = [line["len"] for line in group]
    mean_length = sum(lengths) / len(lengths)
    length_variance = sum(
        (length - mean_length) ** 2 for length in lengths
    ) / len(lengths)
    if math.sqrt(length_variance) / mean_length >= 0.50:
        return set()
    return {line[id_key] for line in group}


def _literal_all_pairs(lines):
    used = [False] * len(lines)
    groups = []
    reports = []
    drawing_ids = set()
    primitive_ids = set()
    for seed_index, seed in enumerate(lines):
        if used[seed_index]:
            continue
        group = [seed]
        used[seed_index] = True
        for candidate_index, candidate in enumerate(lines):
            if candidate_index <= seed_index or used[candidate_index]:
                continue
            if hatch_detector._angle_diff(seed["angle"], candidate["angle"]) < 3.0:
                group.append(candidate)
                used[candidate_index] = True
        accepted_drawings = _literal_accept(group, "idx")
        accepted_primitives = _literal_accept(group, "pid")
        source_order = tuple(line["source_index"] for line in group)
        groups.append(source_order)
        reports.append(
            {
                "seed_index": seed_index,
                "source_order": source_order,
                "drawing_ids": tuple(sorted(accepted_drawings)),
                "primitive_ids": tuple(sorted(accepted_primitives)),
            }
        )
        drawing_ids.update(accepted_drawings)
        primitive_ids.update(accepted_primitives)
    return groups, used, reports, drawing_ids, primitive_ids


def _indexed_result(lines):
    used = [False] * len(lines)
    groups = []
    reports = []
    drawing_ids = set()
    primitive_ids = set()
    bins = hatch_detector._build_angle_bins(lines)
    for seed_index, _seed in enumerate(lines):
        if used[seed_index]:
            continue
        group = hatch_detector._group_parallel_lines(lines, used, seed_index, bins)
        accepted_drawings = hatch_detector._accept_hatch_group(group, "idx")
        accepted_primitives = hatch_detector._accept_hatch_group(group, "pid")
        source_order = tuple(line["source_index"] for line in group)
        groups.append(source_order)
        reports.append(
            {
                "seed_index": seed_index,
                "source_order": source_order,
                "drawing_ids": tuple(sorted(accepted_drawings)),
                "primitive_ids": tuple(sorted(accepted_primitives)),
            }
        )
        drawing_ids.update(accepted_drawings)
        primitive_ids.update(accepted_primitives)
    return groups, used, reports, drawing_ids, primitive_ids


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (0.0, 180.0),
        (0.0, -1e-12),
        (180.0, 1e-12),
        (2.999999999, 0.0),
        (177.000000001, 0.0),
    ],
)
def test_wrapped_seam_candidate_is_present(left, right):
    lines = [_line(left, 0), _line(right, 1)]
    bins = hatch_detector._build_angle_bins(lines)

    assert 1 in hatch_detector._candidate_line_indices(0, lines, bins)


def test_index_matches_literal_oracle_seeded():
    rng = random.Random(20260810)
    clusters = [
        (0.0, [0.0, 180.0, -1e-12, 1e-12, 179.999999999, 2.999999999, 177.000000001]),
        (37.0, [37.0] + [37.0 + rng.uniform(-2.5, 2.5) for _ in range(6)]),
        (83.0, [83.0] + [83.0 + rng.uniform(-2.5, 2.5) for _ in range(6)]),
        (129.0, [129.0] + [129.0 + rng.uniform(-2.5, 2.5) for _ in range(6)]),
    ]
    lines = []
    for group_position in range(7):
        for base, angles in clusters:
            source_index = len(lines)
            lines.append(_line(angles[group_position], source_index, group_position, base))

    expected = _literal_all_pairs(lines)
    actual = _indexed_result(lines)

    assert actual == expected


def test_exact_three_degree_is_rejected():
    lines = [_line(0.0, 0), _line(3.0, 1)]
    used = [False, False]
    bins = hatch_detector._build_angle_bins(lines)

    assert 1 in hatch_detector._candidate_line_indices(0, lines, bins)
    group = hatch_detector._group_parallel_lines(lines, used, 0, bins)
    assert tuple(line["source_index"] for line in group) == (0,)
    assert used == [True, False]
