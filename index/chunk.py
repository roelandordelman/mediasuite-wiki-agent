#!/usr/bin/env python3
"""
Split cleaned articles into retrieval chunks.

Reads:  data/cleaned/{pageid}.json   (plain text)
        data/structured/{pageid}.json (infobox fields + GTAA URI)
Writes: data/chunks/{pageid}.json    (list of chunk dicts, no embeddings yet)

Chunking strategy:
  - Chunk 0 (is_infobox=True): a structured summary built from infobox fields,
    so person/production facts are retrievable even without matching the narrative.
  - Chunks 1..N: sliding window over the plain text, ~800 chars with 100-char overlap.

Redirects and empty articles are skipped.
Re-running is safe: already-chunked files are skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CHUNK_SIZE = 800
OVERLAP = 100


# ── Infobox summary chunk ──────────────────────────────────────────────────────

def _persoon_summary(title: str, p: dict) -> str:
    parts = [f"Naam: {p.get('naam') or title}."]
    if p.get("birth_date"):
        born = p["birth_date"]
        if p.get("birth_place"):
            born += f", {p['birth_place']}"
        parts.append(f"Geboren: {born}.")
    if p.get("death_date"):
        died = p["death_date"]
        if p.get("death_place"):
            died += f", {p['death_place']}"
        parts.append(f"Overleden: {died}.")
    if p.get("functions"):
        parts.append(f"Functies: {', '.join(p['functions'])}.")
    if p.get("period_start"):
        period = str(p["period_start"])
        if p.get("period_end"):
            period += f"–{p['period_end']}"
        parts.append(f"Actief: {period}.")
    if p.get("collaborators"):
        parts.append(f"Werkt samen met: {', '.join(p['collaborators'][:5])}.")
    if p.get("known_for"):
        parts.append(f"Bekend van: {', '.join(p['known_for'][:5])}.")
    return " ".join(parts)


def _productie_summary(title: str, p: dict) -> str:
    parts = [f"Productie: {title}."]
    if p.get("genre"):
        parts.append(f"Genre: {', '.join(p['genre'])}.")
    if p.get("medium"):
        parts.append(f"Medium: {p['medium']}.")
    if p.get("period_start"):
        period = str(p["period_start"])
        if p.get("period_end") and p["period_end"] != p["period_start"]:
            period += f"–{p['period_end']}"
        parts.append(f"Periode: {period}.")
    if p.get("persons"):
        parts.append(f"Betrokkenen: {', '.join(p['persons'][:8])}.")
    return " ".join(parts)


def _infobox_summary(structured: dict) -> str | None:
    article_type = structured.get("article_type")
    title = structured.get("title", "")
    if article_type == "persoon" and structured.get("persoon"):
        return _persoon_summary(title, structured["persoon"])
    if article_type == "productie" and structured.get("productie"):
        return _productie_summary(title, structured["productie"])
    return None


# ── Text chunking ──────────────────────────────────────────────────────────────

def _text_chunks(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> list[str]:
    """Split text into overlapping chunks, breaking on whitespace."""
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end < len(text):
            # Walk back to nearest whitespace so we don't cut mid-word
            while end > start + chunk_size // 2 and not text[end].isspace():
                end -= 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end - overlap
        if start >= len(text):
            break
    return chunks


# ── Per-article chunker ────────────────────────────────────────────────────────

def chunk_article(cleaned: dict, structured: dict) -> list[dict]:
    if cleaned.get("is_redirect") or not cleaned.get("text", "").strip():
        return []

    base = {
        "pageid": cleaned["pageid"],
        "title": cleaned["title"],
        "url": cleaned["url"],
        "categories": cleaned.get("categories", []),
        "last_edited": cleaned.get("last_edited", ""),
        "article_type": structured.get("article_type", "other"),
        "gtaa_uri": structured.get("gtaa_uri"),
    }

    chunks: list[dict] = []

    # Chunk 0: structured infobox summary (if available)
    summary = _infobox_summary(structured)
    if summary:
        chunks.append({
            **base,
            "chunk_id": f"{cleaned['pageid']}_0",
            "chunk_index": 0,
            "is_infobox": True,
            "chunk_text": summary,
        })

    # Chunks 1..N: sliding window over plain text
    text_chunks = _text_chunks(cleaned["text"])
    offset = len(chunks)  # 1 if we added an infobox chunk, else 0
    for i, text in enumerate(text_chunks):
        chunks.append({
            **base,
            "chunk_id": f"{cleaned['pageid']}_{offset + i}",
            "chunk_index": offset + i,
            "is_infobox": False,
            "chunk_text": text,
        })

    return chunks


# ── Runner ─────────────────────────────────────────────────────────────────────

def run(cleaned_dir: Path, structured_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(cleaned_dir.glob("*.json"))
    total = len(files)
    done = skipped = errors = total_chunks = 0

    for i, path in enumerate(files):
        out_path = output_dir / path.name
        if out_path.exists():
            skipped += 1
            continue
        try:
            cleaned = json.loads(path.read_text())
            struct_path = structured_dir / path.name
            structured = json.loads(struct_path.read_text()) if struct_path.exists() else {}
            chunks = chunk_article(cleaned, structured)
            out_path.write_text(json.dumps(chunks, ensure_ascii=False, indent=2))
            total_chunks += len(chunks)
            done += 1
        except Exception as exc:
            log.error("Failed %s: %s", path.name, exc)
            errors += 1

        if (i + 1) % 2000 == 0 or i + 1 == total:
            log.info("Progress: %d / %d | done %d | skipped %d | chunks %d | errors %d",
                     i + 1, total, done, skipped, total_chunks, errors)

    log.info("Done. %d articles → %d chunks (%d skipped, %d errors).",
             done, total_chunks, skipped, errors)


def main() -> None:
    parser = argparse.ArgumentParser(description="Split cleaned articles into retrieval chunks")
    parser.add_argument("--cleaned-dir", default="data/cleaned", type=Path)
    parser.add_argument("--structured-dir", default="data/structured", type=Path)
    parser.add_argument("--output-dir", default="data/chunks", type=Path)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    run(args.cleaned_dir, args.structured_dir, args.output_dir)


if __name__ == "__main__":
    main()
