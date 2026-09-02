"""Category resolution must yield values the bot can map to a numeric path."""
import pytest

from services.categories import (
    CATEGORY_LIST,
    DEFAULT_CATEGORY,
    canonical_category,
    known_categories,
    resolve_category,
)


def test_llm_category_list_is_resolvable() -> None:
    """Every offered category must exist verbatim in the bot's categories.yaml."""
    categories = known_categories()
    invalid = [c for c in CATEGORY_LIST if c not in categories]
    assert invalid == []


def test_default_category_is_resolvable() -> None:
    assert DEFAULT_CATEGORY in known_categories()


def test_leaf_only_name_is_expanded() -> None:
    """Downloaded ads carry the leaf name only."""
    assert resolve_category("Bücher & Zeitschriften") == "Musik, Filme & Bücher > Bücher & Zeitschriften"


def test_known_path_is_kept() -> None:
    assert resolve_category("Elektronik > Notebooks") == "Elektronik > Notebooks"


def test_unknown_child_with_known_parent_is_kept() -> None:
    """The bot itself falls back to the parent path."""
    assert resolve_category("Elektronik > Voellig Erfunden") == "Elektronik > Voellig Erfunden"


def test_unresolvable_returns_none() -> None:
    assert resolve_category("Voellig Erfunden") is None
    assert resolve_category("") is None
    assert resolve_category(None) is None


def test_canonical_falls_back_to_default() -> None:
    assert canonical_category("Voellig Erfunden") == DEFAULT_CATEGORY
    assert canonical_category(None) == DEFAULT_CATEGORY


def test_sanitize_ad_yaml_canonicalizes_category() -> None:
    from services.kleinanzeigen import KleinanzeigenService

    clean = KleinanzeigenService._sanitize_ad_yaml(
        {"title": "x", "category": "Bücher & Zeitschriften", "bogus": 1}
    )
    assert clean["category"] == "Musik, Filme & Bücher > Bücher & Zeitschriften"
    assert "bogus" not in clean
