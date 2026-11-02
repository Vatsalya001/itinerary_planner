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


def test_vegan_and_jain_are_both_kept():
    assert sorted(keys("vegan and jain")) == ["diet:jain", "diet:vegan"]
    merged = merge_constraints(map_constraint_text("vegetarian"), map_constraint_text("jain"))
    assert [c.key for c in merged] == ["diet:jain"]


@pytest.mark.parametrize(
    "text",
    ["I'm not vegetarian", "non-veg is fine", "the venue is accessible by metro", "I'm not busy on Saturday"],
)
def test_constraint_false_positives(text):
    assert keys(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [("must be wheelchair accessible", ["access:wheelchair"]), ("fully accessible venues please", ["access:wheelchair"]),
     ("I can't climb stairs", ["access:wheelchair"]), ("hate crowds", ["crowd:avoid"]), ("not too crowded", ["crowd:avoid"])],
)
def test_constraint_true_positives(text, expected):
    assert keys(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("no non-veg", ["diet:vegetarian"]),
        ("pure veg please, no non-veg places", ["diet:vegetarian"]),
        ("I'm vegetarian, not vegan", ["diet:vegetarian"]),
        ("not a vegetarian place please, I'm vegan", ["diet:vegan"]),
        ("I'm not vegetarian", []),
        ("I eat meat", []),
    ],
)
def test_diet_negation_is_per_word(text, expected):
    assert keys(text) == expected


def test_negates_all_diets():
    from app.domain.constraints import negates_all_diets

    assert negates_all_diets("I'm not vegetarian")
    assert not negates_all_diets("I'm vegetarian, not vegan")
    assert not negates_all_diets("no non-veg")
