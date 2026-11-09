"""Find the ISBN of a book listing; Vinted refuses books without one.

Order: ISBN in the listing text, ISBN readable on a photo (back cover / barcode),
then a catalogue search (Deutsche Nationalbibliothek SRU, Open Library) with the
title words and author an LLM extracted. An LLM picks the matching printed edition
among real catalogue hits or none. Every ISBN is checksum-verified; nothing is
guessed. Kleinanzeigen titles are cut at 50 characters ("... - P" for Pirker), so
the search uses keywords instead of the exact title.
"""
from __future__ import annotations

import base64
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from services.vinted_mapper import find_isbn, normalize_isbn, parse_llm_json, to_jpeg

logger = logging.getLogger(__name__)

DNB_SRU_URL = "https://services.dnb.de/sru/dnb"
OPEN_LIBRARY_URL = "https://openlibrary.org/search.json"
USER_AGENT = "kleinanzeigen-telegram-bot (+https://github.com/airbone42/kleinanzeigen-bot)"
MAX_CANDIDATES = 12
PHOTOS_FOR_ISBN = 4
_STOPWORDS = {
    "buch", "bücher", "und", "der", "die", "das", "ein", "eine", "wie", "mit", "von", "für",
    "the", "and", "neu", "neuwertig", "gebunden", "taschenbuch", "auflage", "band",
}
_DC = "{http://purl.org/dc/elements/1.1/}"
_EBOOK_RE = re.compile(r"e-?book|epub|pdf|online", re.IGNORECASE)
_PRINT_RE = re.compile(r"festeinband|gb\b|kart|pb\b|paperback|taschenbuch|broschur|eur\b", re.IGNORECASE)


@dataclass
class BookCandidate:
    title: str
    isbn: str
    note: str = ""
    source: str = ""

    def line(self) -> str:
        return f"{self.isbn} | {self.title[:160]} | {self.note[:60]} | {self.source}"


def keywords(*texts: str, limit: int = 6) -> list[str]:
    words: list[str] = []
    for text in texts:
        for word in re.findall(r"[\wÄÖÜäöüß]{4,}", text or ""):
            low = word.lower()
            if low not in _STOPWORDS and low not in (w.lower() for w in words):
                words.append(word)
    return words[:limit]


def parse_dnb_records(xml_text: str) -> list[BookCandidate]:
    """oai_dc records of a DNB SRU answer -> candidates (one per ISBN)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out: list[BookCandidate] = []
    for record in root.iter():
        if not record.tag.endswith("}dc") and record.tag != "dc":
            continue
        title = " ".join((t.text or "").strip() for t in record.findall(f"{_DC}title"))
        ebook = any(
            "tel:URN" in (i.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type") or "")
            for i in record.findall(f"{_DC}identifier")
        )
        for ident in record.findall(f"{_DC}identifier"):
            kind = ident.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type") or ""
            if not kind.endswith("ISBN"):
                continue
            raw = (ident.text or "").strip()
            isbn = normalize_isbn(raw.split(" ")[0])
            if isbn:
                note = raw[len(raw.split(" ")[0]):].strip()
                if ebook and not note:
                    note = "E-Book"
                out.append(BookCandidate(title=title, isbn=isbn, note=note, source="DNB"))
    return out


async def search_dnb(words: list[str], http: httpx.AsyncClient) -> list[BookCandidate]:
    if not words:
        return []
    query = " and ".join(words)
    resp = await http.get(DNB_SRU_URL, params={
        "version": "1.1", "operation": "searchRetrieve", "query": query,
        "recordSchema": "oai_dc", "maximumRecords": "10",
    })
    return parse_dnb_records(resp.text) if resp.status_code == 200 else []


async def search_open_library(words: list[str], http: httpx.AsyncClient) -> list[BookCandidate]:
    if not words:
        return []
    resp = await http.get(OPEN_LIBRARY_URL, params={
        "q": " ".join(words), "fields": "title,author_name,isbn", "limit": "5",
    })
    if resp.status_code != 200:
        return []
    out: list[BookCandidate] = []
    for doc in resp.json().get("docs") or []:
        title = f"{doc.get('title') or ''} / {', '.join(doc.get('author_name') or [])}"
        for raw in (doc.get("isbn") or [])[:3]:
            isbn = normalize_isbn(str(raw))
            if isbn:
                out.append(BookCandidate(title=title, isbn=isbn, source="Open Library"))
    return out


async def isbn_from_photos(vision_llm: Any, images: list[bytes]) -> Optional[str]:
    """Read an ISBN/EAN printed on a photo (usually the back cover)."""
    from langchain_core.messages import HumanMessage

    if not images:
        return None
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": ("Steht auf einem der Fotos eine ISBN oder eine EAN unter einem Barcode eines "
                 "Buches? Antworte nur mit JSON {\"isbn\": \"<Ziffern>\"} oder {\"isbn\": null}. "
                 "Nur Ziffern, die wirklich lesbar sind, nichts ergänzen."),
    }]
    # Back covers are usually the last photos.
    for image in list(reversed(images))[:PHOTOS_FOR_ISBN]:
        b64 = base64.b64encode(to_jpeg(image)).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    answer = await vision_llm.ainvoke([HumanMessage(content=content)])  # type: ignore[arg-type]
    return normalize_isbn(parse_llm_json(str(getattr(answer, "content", answer))).get("isbn"))


async def book_query(llm: Any, title: str, description: str) -> tuple[str, str]:
    """Full book title and author as far as title and description reveal them."""
    from langchain_core.messages import HumanMessage, SystemMessage

    answer = await llm.ainvoke([
        SystemMessage(content=(
            "Aus einer Kleinanzeige für ein Buch: nenne Buchtitel und Autor, soweit sie im Text "
            'stehen (der Anzeigentitel ist oft abgeschnitten). Nur JSON {"title": "...", '
            '"author": "..." oder null}. Nichts erfinden.'
        )),
        HumanMessage(content=f"Anzeigentitel: {title}\nBeschreibung:\n{(description or '')[:1500]}"),
    ])
    data = parse_llm_json(str(getattr(answer, "content", answer)))
    book_title = data.get("title") if isinstance(data.get("title"), str) else ""
    author = data.get("author") if isinstance(data.get("author"), str) else ""
    return book_title or title, author or ""


async def choose_candidate(
    llm: Any, title: str, description: str, candidates: list[BookCandidate]
) -> Optional[str]:
    """Let the LLM pick the matching printed edition among real catalogue hits."""
    from langchain_core.messages import HumanMessage, SystemMessage

    if not candidates:
        return None
    answer = await llm.ainvoke([
        SystemMessage(content=(
            "Welcher Katalogeintrag ist das Buch aus der Kleinanzeige? Nimm die gedruckte "
            "Ausgabe (gebunden/Taschenbuch), kein E-Book, und passend zu Angaben in der Anzeige. "
            'Antworte nur mit JSON {"isbn": "<ISBN aus der Liste>"} oder {"isbn": null}, '
            "wenn keiner sicher passt."
        )),
        HumanMessage(content=(
            f"Anzeigentitel: {title}\nBeschreibung:\n{(description or '')[:1200]}\n\n"
            "Katalog (ISBN | Titel / Autor | Ausgabe | Quelle):\n"
            + "\n".join(c.line() for c in candidates[:MAX_CANDIDATES])
        )),
    ])
    picked = normalize_isbn(parse_llm_json(str(getattr(answer, "content", answer))).get("isbn"))
    return picked if picked in {c.isbn for c in candidates} else None


def rank_candidates(candidates: list[BookCandidate]) -> list[BookCandidate]:
    """Unique ISBNs, printed editions first, e-books last."""
    seen: set[str] = set()
    unique = []
    for cand in candidates:
        if cand.isbn not in seen:
            seen.add(cand.isbn)
            unique.append(cand)

    def score(c: BookCandidate) -> int:
        if _EBOOK_RE.search(c.note):
            return 2
        return 0 if _PRINT_RE.search(c.note) else 1

    return sorted(unique, key=score)


async def find_book_isbn(
    *,
    text_llm: Any,
    vision_llm: Any,
    title: str,
    description: str,
    images: list[bytes],
) -> tuple[Optional[str], str]:
    """Returns (isbn, source) or (None, "")."""
    isbn = find_isbn(description) or find_isbn(title)
    if isbn:
        return isbn, "Anzeigentext"
    try:
        isbn = await isbn_from_photos(vision_llm, images)
        if isbn:
            return isbn, "Foto"
    except Exception as e:
        logger.info(f"ISBN from photos failed: {e}")
    try:
        book_title, author = await book_query(text_llm, title, description)
        words = keywords(book_title, author)
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as http:
            candidates = await search_dnb(words, http)
            if len(words) > 3 and not candidates:
                candidates = await search_dnb(words[:3], http)
            candidates += await search_open_library(words, http)
        isbn = await choose_candidate(text_llm, title, description, rank_candidates(candidates))
        if isbn:
            return isbn, "Katalog (DNB/Open Library)"
    except Exception as e:
        logger.info(f"ISBN catalogue lookup failed for {title!r}: {e}")
    return None, ""
