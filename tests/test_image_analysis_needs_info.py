"""Image analysis must ask the seller instead of guessing or publishing a placeholder."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.listing_creator import _route_after_analysis
from agents.nodes.image_analysis import _build_result, analyze_images


def test_confident_json_gives_analysis_without_question() -> None:
    raw = (
        '{"produkt": "Ubiquiti UniFi Security Gateway USG", "zustand": "Gut erhalten", '
        '"besonderheiten": "12V Netzteil", "sicherheit": "hoch", "rueckfragen": []}'
    )
    result = _build_result(raw)
    assert result["needs_info"] == ""
    assert "UniFi Security Gateway" in result["image_analysis"]


def test_low_confidence_asks_questions() -> None:
    raw = (
        '{"produkt": "weisses Gehaeuse", "zustand": "Gebraucht", "besonderheiten": "", '
        '"sicherheit": "niedrig", "rueckfragen": ["Welches Modell ist das?"]}'
    )
    result = _build_result(raw)
    assert result["image_analysis"] == ""
    assert "Welches Modell" in result["needs_info"]


@pytest.mark.parametrize("raw", ["", "ok", "{kaputt"])
def test_unusable_reply_asks_instead_of_guessing(raw: str) -> None:
    assert _build_result(raw)["needs_info"]


@pytest.mark.asyncio
async def test_llm_failure_asks_and_does_not_leak_error_text() -> None:
    state = {"images": ["abc"], "caption": ""}
    with patch("agents.nodes.image_analysis.get_vision_llm") as get_llm:
        llm = AsyncMock()
        llm.ainvoke.side_effect = RuntimeError("boom")
        get_llm.return_value = llm
        result = await analyze_images(state)  # type: ignore[arg-type]
    assert result["needs_info"]
    assert "boom" not in result["image_analysis"]


@pytest.mark.asyncio
async def test_empty_model_reply_asks() -> None:
    state = {"images": ["abc"], "caption": ""}
    response = MagicMock()
    response.content = ""
    with patch("agents.nodes.image_analysis.get_vision_llm") as get_llm:
        llm = AsyncMock()
        llm.ainvoke.return_value = response
        get_llm.return_value = llm
        result = await analyze_images(state)  # type: ignore[arg-type]
    assert result["needs_info"]


def test_router() -> None:
    assert _route_after_analysis({"needs_info": "• Frage"}) == ["__end__"]  # type: ignore[typeddict-item]
    assert _route_after_analysis({"needs_info": ""}) == ["generate_title", "generate_category", "estimate_price"]  # type: ignore[typeddict-item]


@pytest.mark.asyncio
async def test_graph_stops_before_publishable_draft_on_low_confidence() -> None:
    from agents.listing_creator import build_listing_creator_graph

    response = MagicMock()
    response.content = '{"produkt": "", "zustand": "", "sicherheit": "niedrig", "rueckfragen": ["Welches Modell?"]}'
    with patch("agents.nodes.image_analysis.get_vision_llm") as get_llm:
        llm = AsyncMock()
        llm.ainvoke.return_value = response
        get_llm.return_value = llm
        result = await build_listing_creator_graph().ainvoke(
            {"images": ["abc"], "caption": "", "title": "", "description": "", "price": 0.0}
        )
    assert "Welches Modell" in result["needs_info"]
    assert not result["title"]
