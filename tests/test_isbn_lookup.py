"""ISBN lookup for book listings (Vinted requires an ISBN for books)."""
from types import SimpleNamespace
from typing import Any

from services import isbn_lookup as il

DNB_XML = """<?xml version="1.0"?>
<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/"><records>
<record><recordData><dc xmlns="http://www.openarchives.org/OAI/2.0/oai_dc/"
  xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:title>The Game is On - Wie Gaming unsere Welt revolutioniert / Johanna Pirker</dc:title>
<dc:identifier xsi:type="tel:URN">urn:nbn:de:101:1-1</dc:identifier>
<dc:identifier xsi:type="tel:ISBN">978-3-69128-279-5</dc:identifier>
</dc></recordData></record>
<record><recordData><dc xmlns="http://www.openarchives.org/OAI/2.0/oai_dc/"
  xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:title>The game is on : wie Gaming unsere Welt revolutioniert / Johanna Pirker</dc:title>
<dc:identifier xsi:type="tel:ISBN">978-3-7110-0369-0 Festeinband : EUR 26.00 (DE)</dc:identifier>
</dc></recordData></record>
</records></searchRetrieveResponse>"""


def test_parse_dnb_and_rank_prefers_print() -> None:
    ranked = il.rank_candidates(il.parse_dnb_records(DNB_XML))
    assert [c.isbn for c in ranked] == ["9783711003690", "9783691282795"]
    assert ranked[1].note == "E-Book"


def test_keywords_skip_stopwords() -> None:
    assert il.keywords("Buch: Gaming revolutioniert die Welt", "P") == ["Gaming", "revolutioniert", "Welt"]


class FakeLLM:
    def __init__(self, answers: list[str]) -> None:
        self.answers = answers

    async def ainvoke(self, messages: Any) -> Any:
        return SimpleNamespace(content=self.answers.pop(0))


async def test_find_book_isbn_text_photo_catalogue(monkeypatch) -> None:
    isbn, src = await il.find_book_isbn(
        text_llm=None, vision_llm=None, title="Roman", description="ISBN 978-3-16-148410-0",
        images=[],
    )
    assert (isbn, src) == ("9783161484100", "Anzeigentext")

    isbn, src = await il.find_book_isbn(
        text_llm=None, vision_llm=FakeLLM(['{"isbn": "9783711003690"}']), title="Buch", description="",
        images=[b"\xff\xd8\xffjpeg"],
    )
    assert (isbn, src) == ("9783711003690", "Foto")

    async def fake_dnb(words, http):
        return il.parse_dnb_records(DNB_XML)

    async def fake_ol(words, http):
        return []

    monkeypatch.setattr(il, "search_dnb", fake_dnb)
    monkeypatch.setattr(il, "search_open_library", fake_ol)
    text_llm = FakeLLM([
        '{"title": "Gaming revolutioniert die Welt", "author": "Pirker"}',
        '{"isbn": "9783711003690"}',
    ])
    isbn, src = await il.find_book_isbn(
        text_llm=text_llm, vision_llm=FakeLLM(['{"isbn": null}']),
        title="Buch: Gaming revolutioniert die Welt - P", description="Sachbuch", images=[b"x"],
    )
    assert isbn == "9783711003690" and "Katalog" in src


async def test_choose_candidate_rejects_isbn_not_in_list() -> None:
    cands = il.parse_dnb_records(DNB_XML)
    assert await il.choose_candidate(FakeLLM(['{"isbn": "9783161484100"}']), "x", "", cands) is None
