#!/usr/bin/env python3
"""
MCP server for the Beeld & Geluid Wiki agent.

Tools:
  wiki_search(query, limit)  — semantic search over the Milvus index
  wiki_lookup(title)         — exact/near-exact title lookup
  wiki_metadata(title)       — article metadata without content

Configuration via environment variables:
  MILVUS_URI   path or URL (default: data/milvus_wiki.db)

Run:
  python3.12 mcp/server.py          # stdio transport (for MCP clients)
  python3.12 mcp/server.py --test   # smoke-test a live search and exit

Swap to production:
  MILVUS_URI=http://milvus:19530 python3.12 mcp/server.py
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pymilvus import MilvusClient

MILVUS_URI = os.getenv("MILVUS_URI", str(Path(__file__).parent.parent / "data" / "milvus_wiki.db"))
COLLECTION = "mediasuite_wiki"
EMBEDDING_MODEL = "intfloat/multilingual-e5-large-instruct"

mcp = FastMCP("mediasuite-wiki-agent")

# ── Singletons (lazy-initialised) ─────────────────────────────────────────────

_client: MilvusClient | None = None
_embedder = None


def _get_client() -> MilvusClient:
    global _client
    if _client is None:
        _client = MilvusClient(uri=MILVUS_URI)
    return _client


def _get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(EMBEDDING_MODEL)
    return _embedder


def _embed_query(query: str) -> list[float]:
    vec = _get_embedder().encode(
        [f"query: {query}"], normalize_embeddings=True, show_progress_bar=False
    )
    return vec[0].tolist()


def _escape(s: str) -> str:
    """Escape double quotes in a string for use in a Milvus filter expression."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _staleness_note(last_edited: str) -> str | None:
    """Return a warning string if the article hasn't been edited in over 2 years."""
    try:
        edited = datetime.fromisoformat(last_edited.replace("Z", "+00:00"))
        age_years = (datetime.now(timezone.utc) - edited).days / 365
        if age_years > 2:
            return f"Let op: dit artikel is meer dan {int(age_years)} jaar niet bijgewerkt en kan verouderde informatie bevatten."
    except Exception:
        pass
    return None


SOURCE_NOTE = (
    "Bron: Beeld & Geluid Wiki (wiki.beeldengeluid.nl), "
    "onderhouden door medewerkers en vrijwilligers van Sound and Vision. "
    "Inhoud kan verouderd zijn."
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def wiki_search(query: str, limit: int = 5) -> list[dict]:
    """
    Semantic search over the Beeld & Geluid Wiki.

    Use for open-ended questions about Dutch media history: persons, productions,
    genres, topics, decades. Returns ranked chunks with source URL and
    last-edited timestamp.

    Args:
        query: Natural language question or search terms (Dutch or English).
        limit: Number of results to return (default 5, max 20).
    """
    limit = min(limit, 20)
    vec = _embed_query(query)
    raw = _get_client().search(
        collection_name=COLLECTION,
        data=[vec],
        limit=limit,
        output_fields=[
            "title", "url", "chunk_text", "last_edited",
            "gtaa_uri", "article_type", "is_infobox",
        ],
        search_params={"metric_type": "COSINE"},
    )
    results = []
    for r in raw[0]:
        e = r["entity"]
        item = {
            "title": e["title"],
            "url": e["url"],
            "excerpt": e["chunk_text"],
            "last_edited": e["last_edited"],
            "score": round(r["distance"], 4),
            "source_note": SOURCE_NOTE,
        }
        if e.get("gtaa_uri"):
            item["gtaa_uri"] = e["gtaa_uri"]
        stale = _staleness_note(e["last_edited"])
        if stale:
            item["staleness_warning"] = stale
        results.append(item)
    return results


@mcp.tool()
def wiki_lookup(title: str) -> dict | None:
    """
    Look up a specific person, production, or topic by name.

    Tries an exact title match first. If not found, falls back to semantic search.
    Returns the structured infobox summary and lead text.

    Use when a specific name is known, e.g. when a researcher clicks on a search
    result and wants background context.

    Args:
        title: Article title, person name, or production name.
    """
    client = _get_client()
    escaped = _escape(title)

    # 1. Exact match: prefer the infobox chunk, fall back to first chunk
    for filt in [
        f'title == "{escaped}" and is_infobox == True',
        f'title == "{escaped}" and chunk_index == 0',
    ]:
        rows = client.query(
            collection_name=COLLECTION,
            filter=filt,
            output_fields=[
                "pageid", "title", "url", "chunk_text",
                "last_edited", "gtaa_uri", "article_type", "categories",
            ],
            limit=1,
        )
        if rows:
            r = rows[0]
            # Also fetch the first narrative chunk if we got the infobox
            lead = r["chunk_text"]
            if r.get("is_infobox"):
                narrative = client.query(
                    collection_name=COLLECTION,
                    filter=f'title == "{escaped}" and chunk_index == 1',
                    output_fields=["chunk_text"],
                    limit=1,
                )
                if narrative:
                    lead = r["chunk_text"] + "\n\n" + narrative[0]["chunk_text"]

            try:
                cats = json.loads(r["categories"])
            except Exception:
                cats = []

            result = {
                "title": r["title"],
                "url": r["url"],
                "summary": lead,
                "article_type": r["article_type"],
                "categories": cats,
                "last_edited": r["last_edited"],
                "source_note": SOURCE_NOTE,
            }
            if r.get("gtaa_uri"):
                result["gtaa_uri"] = r["gtaa_uri"]
            stale = _staleness_note(r["last_edited"])
            if stale:
                result["staleness_warning"] = stale
            return result

    # 2. Semantic fallback
    results = wiki_search(title, limit=1)
    if results:
        return {**results[0], "match_type": "semantic_fallback"}

    return None


@mcp.tool()
def wiki_metadata(title: str) -> dict | None:
    """
    Return metadata for a wiki article without its content.

    Useful for checking whether wiki information is current before presenting
    it to a researcher, or for resolving a GTAA URI from a person/production name.

    Args:
        title: Exact article title.
    """
    client = _get_client()
    escaped = _escape(title)
    rows = client.query(
        collection_name=COLLECTION,
        filter=f'title == "{escaped}" and chunk_index == 0',
        output_fields=[
            "pageid", "title", "url", "categories",
            "last_edited", "gtaa_uri", "article_type",
        ],
        limit=1,
    )
    if not rows:
        return None

    r = rows[0]
    try:
        cats = json.loads(r["categories"])
    except Exception:
        cats = []

    result = {
        "pageid": r["pageid"],
        "title": r["title"],
        "url": r["url"],
        "article_type": r["article_type"],
        "categories": cats,
        "last_edited": r["last_edited"],
        "source_note": SOURCE_NOTE,
    }
    if r.get("gtaa_uri"):
        result["gtaa_uri"] = r["gtaa_uri"]
    stale = _staleness_note(r["last_edited"])
    if stale:
        result["staleness_warning"] = stale
    return result


# ── Smoke test ─────────────────────────────────────────────────────────────────

def _smoke_test() -> None:
    import pprint
    print("=== wiki_search('Rob de Nijs') ===")
    pprint.pprint(wiki_search("Rob de Nijs", limit=2))

    print("\n=== wiki_lookup('Rob de Nijs') ===")
    pprint.pprint(wiki_lookup("Rob de Nijs"))

    print("\n=== wiki_metadata('Rob de Nijs') ===")
    pprint.pprint(wiki_metadata("Rob de Nijs"))


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Run smoke test and exit")
    args = parser.parse_args()

    if args.test:
        _smoke_test()
    else:
        mcp.run()
