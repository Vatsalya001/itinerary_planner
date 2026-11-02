"""Canonical place categories loaded from data/categories.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class CategoryInfo(BaseModel):
    key: str
    group: str
    meal: bool
    indoor: bool
    seated: bool
    effort: Literal["low", "medium", "high"]
    walking: bool
    duration: tuple[int, int, int]
    alcohol: bool
    cost_inr: tuple[int, int]
    tags: list[str]
    busy: list[tuple[int, int]]


@lru_cache(maxsize=1)
def load_categories() -> dict[str, CategoryInfo]:
    raw = yaml.safe_load((DATA_DIR / "categories.yaml").read_text(encoding="utf-8"))
    return {key: CategoryInfo(key=key, **value) for key, value in raw.items()}


def category(key: str) -> CategoryInfo:
    """Return the category; unknown keys raise KeyError so provider mapping bugs surface early."""
    return load_categories()[key]


def is_category(key: str) -> bool:
    return key in load_categories()


@lru_cache(maxsize=1)
def load_countries() -> dict[str, dict[str, str]]:
    return yaml.safe_load((DATA_DIR / "countries.yaml").read_text(encoding="utf-8"))


def currency_for_country(country_code: str) -> str | None:
    entry = load_countries().get(country_code.upper())
    return entry["currency"] if entry else None


def currency_symbol(currency: str) -> str:
    for entry in load_countries().values():
        if entry["currency"] == currency:
            return entry["symbol"]
    return f"{currency} "
