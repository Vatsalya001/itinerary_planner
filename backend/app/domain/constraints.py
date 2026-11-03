"""Hard-constraint vocabulary, rule-based mapping from text, and conflict detection.

The vocabulary is deliberately small so every constraint has a precise, checkable meaning. Text that
sounds like a hard requirement but maps to nothing becomes a clarification or a disclosed soft preference.
"""

from __future__ import annotations

import re

from app.domain.schemas import Constraint, ConstraintConflict

# kind -> allowed values. must_include / must_avoid values are categories or tags (see categories.yaml).
VOCABULARY: dict[str, set[str]] = {
    "diet": {"vegetarian", "vegan", "jain"},
    "alcohol": {"avoid"},
    "access": {"wheelchair"},
    "crowd": {"avoid"},
    "setting": {"indoors_only", "outdoors_only"},
    "must_include": {
        "sit_down_meal", "outdoor_walk", "live_music", "coffee", "museum", "art", "books",
        "park", "event", "shopping", "dessert",
    },
    "must_avoid": {"mall", "bar", "brewery", "market", "cinema", "shopping", "alcohol"},
}

# (regex, kind, value). Order matters: the first match per rule group wins; several rules may match.
_RULES: list[tuple[str, str, str]] = [
    (r"\bvegan\b|plant[- ]based", "diet", "vegan"),
    (r"\bjain\b", "diet", "jain"),
    (r"(?<!non[- ])(?<!non)\b(pure[- ]?)?veg(etarian|gie|)\b|no (meat|non[- ]?veg)|(?:don[’']?t|do not|never|cannot|can’?t) eat (?:meat|non[- ]?veg|chicken|fish)", "diet", "vegetarian"),
    (r"(no|avoid|without|skip|don'?t want|not)\b.{0,20}\b(alcohol|drinks|bars?|pubs?|booze|breweries)"
     r"|teetotal|sober|alcohol[- ]free", "alcohol", "avoid"),
    (r"wheel ?chair|step[- ]free|mobility (aid|issues?|impair\w*|scooter)|(fully|physically|disab\w*)[- ]accessible"
     r"|accessible (venues?|places?|spots?|for (a )?wheel ?chair)|disabled access|can'?t (do|climb) stairs",
     "access", "wheelchair"),
    (r"(avoid|no|hate|less|away from|without|fewer|dislike)\b.{0,20}\b(crowds?|crowded|busy|packed|rush)"
     r"|uncrowded|not (too )?crowded|less crowded|quiet places", "crowd", "avoid"),
    (r"indoors? only|only indoors?|stay indoors?|no outdoor|nothing outdoors?", "setting", "indoors_only"),
    (r"outdoors? only|only outdoors?|nothing indoors?", "setting", "outdoors_only"),
    (r"sit[- ]down (meal|lunch|dinner|restaurant)|proper (meal|lunch|dinner)|(include|have|want) (a )?(meal|lunch|dinner)",
     "must_include", "sit_down_meal"),
    (r"(include|must|want|need|have)\b.{0,25}\b(outdoor )?walk|outdoor walk", "must_include", "outdoor_walk"),
    (r"(include|must|want|need)\b.{0,25}\blive music", "must_include", "live_music"),
    (r"(include|must|want|need)\b.{0,25}\bmuseum", "must_include", "museum"),
    (r"(avoid|no|skip|not)\b.{0,10}\bmalls?\b", "must_avoid", "mall"),
    (r"(avoid|no|skip|not)\b.{0,10}\bmarkets?\b", "must_avoid", "market"),
]


def map_constraint_text(text: str) -> list[Constraint]:
    """Map one free-text constraint to zero or more vocabulary constraints.

    "vegetarian" -> [diet:vegetarian]; "avoid crowded places" -> [crowd:avoid];
    "vegetarian, wheelchair accessible" -> both. Returns [] when nothing maps.
    """
    lowered = text.lower().replace("’", "'")
    lowered = re.sub(r"no (?:problem|issues?) with", "comfortable with", lowered)
    if re.search(r"veg and non[- ]?veg (?:are )?both fine", lowered):
        lowered = re.sub(r"veg and non[- ]?veg", "all food", lowered)
    found: list[Constraint] = []
    for pattern, kind, value in _RULES:
        if kind == "diet" and diet_negated(lowered, value):
            continue
        if kind == "access" and _access_negated(lowered):
            continue
        if re.search(pattern, lowered):
            c = Constraint(kind=kind, value=value, source_text=text)  # type: ignore[arg-type,unused-ignore]
            if c not in found:
                found.append(c)
    return _collapse_diets(found)


def _access_negated(lowered: str) -> bool:
    """ "no need for wheelchair access", "wheelchair not needed", "don't need step-free" """
    access = r"(wheel ?chair|step[- ]free|accessib\w*|disabled access)"
    return bool(
        re.search(rf"\b(no need for|don'?t need|do not need|not need|no)\s+(a\s+|any\s+)?{access}", lowered)
        or re.search(rf"{access}\s+(access\s+)?(is\s+)?(not|isn'?t)\s+(needed|required|necessary)", lowered)
    )


_DIET_WORDS = {"vegetarian": r"(veg|vegetarian|veggie)", "vegan": r"vegan", "jain": r"jain"}


def diet_negated(text: str, diet: str) -> bool:
    """True when this specific diet word is negated: "I'm not vegetarian", "never vegan", "not a jain place".

    Only the negated word is suppressed, so "I'm vegetarian, not vegan" keeps vegetarian and
    "no non-veg" (= vegetarian) is not a negation. "non-veg is fine" never matches the vegetarian rule.
    """
    word = _DIET_WORDS[diet]
    lowered = text.lower().replace("’", "'")
    if diet == "vegetarian" and re.search(r"\b(?:don'?t|do not|never|cannot|can't) eat (?:meat|non[- ]?veg|chicken|fish)\b", lowered):
        return False
    if re.search(rf"\b(not|never|isn'?t|am not|i'?m not)\s+(a\s+|really\s+|strictly\s+|fully\s+)?{word}\b", lowered):
        return True
    return diet == "vegetarian" and bool(
        re.search(r"\b(i eat|eat|fine with|ok with|okay with|love)\s+(meat|non[- ]?veg|chicken|fish)\b", lowered)
    )


def negates_all_diets(text: str) -> bool:
    """A statement that the user has no diet restriction ("I'm not vegetarian", "I eat meat")."""
    lowered = text.lower()
    return diet_negated(lowered, "vegetarian") and not any(
        re.search(pattern, lowered) and not diet_negated(lowered, value)
        for pattern, kind, value in _RULES if kind == "diet"
    )


def _collapse_diets(constraints: list[Constraint]) -> list[Constraint]:
    """vegan and jain each imply vegetarian, so drop plain vegetarian when a stricter diet is present.
    vegan and jain are different requirements and are both kept."""
    keys = {c.key for c in constraints}
    if "diet:vegetarian" in keys and ({"diet:vegan", "diet:jain"} & keys):
        return [c for c in constraints if c.key != "diet:vegetarian"]
    return constraints


def is_valid(constraint: Constraint) -> bool:
    return constraint.value in VOCABULARY.get(constraint.kind, set())


_OUTDOOR_INCLUDES = {"outdoor_walk", "park"}
_INDOOR_INCLUDES = {"museum", "sit_down_meal", "books", "art", "live_music"}


def detect_conflicts(constraints: list[Constraint]) -> list[ConstraintConflict]:
    """Pairs of hard constraints that cannot both hold. Each conflict triggers a clarification."""
    out: list[ConstraintConflict] = []
    keys = {c.key: c for c in constraints}

    def add(a: str, b: str, reason: str) -> None:
        if a in keys and b in keys:
            out.append(ConstraintConflict(a=keys[a], b=keys[b], reason=reason))

    for inc in _OUTDOOR_INCLUDES:
        add("setting:indoors_only", f"must_include:{inc}", "an indoors-only day can't include an outdoor walk")
    for inc in _INDOOR_INCLUDES - {"sit_down_meal"}:
        add("setting:outdoors_only", f"must_include:{inc}", "an outdoors-only day can't include an indoor venue")
    add("setting:indoors_only", "setting:outdoors_only", "indoors-only and outdoors-only both requested")
    for c in constraints:
        if c.kind == "must_include":
            add(f"must_include:{c.value}", f"must_avoid:{c.value}", f"'{c.value}' is both required and excluded")
    return out


def merge_constraints(*groups: list[Constraint]) -> list[Constraint]:
    """Union preserving first-seen order; vegan/jain replace plain vegetarian (both strict diets are kept)."""
    merged: list[Constraint] = []
    for group in groups:
        for c in group:
            if c not in merged:
                merged.append(c)
    return _collapse_diets(merged)
