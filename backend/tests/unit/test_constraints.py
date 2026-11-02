import pytest

from app.domain.constraints import (
    VOCABULARY,
    detect_conflicts,
    is_valid,
    map_constraint_text,
    merge_constraints,
)
from app.domain.schemas import Constraint


def keys(text):
    return [c.key for c in map_constraint_text(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("vegetarian", ["diet:vegetarian"]),
        ("pure veg only", ["diet:vegetarian"]),
        ("I'm vegan", ["diet:vegan"]),
        ("jain food only", ["diet:jain"]),
        ("non-veg is fine", []),
        ("avoid crowded places", ["crowd:avoid"]),
        ("wheelchair accessible", ["access:wheelchair"]),
        ("no alcohol please", ["alcohol:avoid"]),
        ("indoors only", ["setting:indoors_only"]),
        ("must include an outdoor walk", ["must_include:outdoor_walk"]),
        ("include a sit-down meal", ["must_include:sit_down_meal"]),
        ("no malls", ["must_avoid:mall"]),
        ("bring an umbrella", []),
    ],
)
def test_map_constraint_text(text, expected):
    assert keys(text) == expected


def test_stricter_diet_wins():
    assert keys("vegetarian, actually vegan") == ["diet:vegan"]


def test_all_rule_outputs_are_in_vocabulary():
    samples = ["vegan", "jain", "veg", "no drinks", "wheelchair", "avoid crowds", "indoors only", "outdoors only",
               "sit-down dinner", "want an outdoor walk", "must have live music", "include a museum", "skip malls",
               "avoid markets"]
    for s in samples:
        for c in map_constraint_text(s):
            assert is_valid(c), c.key
            assert c.value in VOCABULARY[c.kind]


def test_conflicts():
    cs = map_constraint_text("indoors only") + map_constraint_text("must include an outdoor walk")
    conflicts = detect_conflicts(cs)
    assert len(conflicts) == 1
    assert {conflicts[0].a.key, conflicts[0].b.key} == {"setting:indoors_only", "must_include:outdoor_walk"}
    assert detect_conflicts(map_constraint_text("vegetarian") + map_constraint_text("avoid crowds")) == []


def test_merge_constraints_union_and_strict_diet():
    a = [Constraint(kind="diet", value="vegetarian"), Constraint(kind="crowd", value="avoid")]
    b = [Constraint(kind="diet", value="vegan")]
    assert [c.key for c in merge_constraints(a, b)] == ["crowd:avoid", "diet:vegan"]
