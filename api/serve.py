#!/usr/bin/env python3
"""
REST API wrapper for the Beeld & Geluid Wiki tools.

Exposes the same four tools as the MCP server as plain JSON REST endpoints,
so the media-suite-learn-chatbot can call them without MCP client complexity.

Run:
  python3.12 -m uvicorn api.serve:app --port 8002

Endpoints:
  GET  /health
  POST /ask       dual-path retrieval: SPARQL + semantic, returns merged context
  POST /search    wiki_search(query, limit)   — raw semantic search
  POST /lookup    wiki_lookup(title)
  POST /metadata  wiki_metadata(title)
  POST /query     wiki_query(query_name, params)
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Import tool implementations from the local mcp/server.py.
# We add the mcp/ subdirectory to sys.path and import 'server' directly to
# avoid shadowing the installed 'mcp' SDK package (which also has mcp.server).
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))
from server import wiki_search as _wiki_search
from server import wiki_lookup as _wiki_lookup
from server import wiki_metadata as _wiki_metadata
from server import wiki_query as _wiki_query

sys.path.insert(0, str(Path(__file__).parent))
from wiki_router import select as _route, format_sparql_results

app = FastAPI(title="Beeld & Geluid Wiki API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


SEMANTIC_MIN_SCORE = 0.70
SEMANTIC_LIMIT = 3


class AskRequest(BaseModel):
    question: str
    top_k: int = SEMANTIC_LIMIT
    min_score: float = SEMANTIC_MIN_SCORE


class SearchRequest(BaseModel):
    query: str
    limit: int = 5


class TitleRequest(BaseModel):
    title: str


class QueryRequest(BaseModel):
    query_name: str
    params: dict = {}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/ask")
def ask(req: AskRequest) -> dict:
    """
    Dual-path retrieval: structured SPARQL + semantic search.

    Runs both paths in parallel (sequentially here, both are fast),
    merges the results into a single context block, and returns it
    alongside a source list and hit counts for transparency.

    The caller (e.g. media-suite-learn-chatbot) passes this context to
    its LLM for final answer generation — no LLM runs inside the wiki agent.
    """
    context_parts: list[str] = []
    sources: list[dict] = []
    sparql_hits = 0

    # ── Structured path ──────────────────────────────────────────────────────
    selections = _route(req.question)
    for query_name, params in selections:
        try:
            rows = _wiki_query(query_name, params)
        except Exception:
            continue
        if rows:
            sparql_hits += len(rows)
            formatted = format_sparql_results(query_name, rows)
            if formatted:
                context_parts.append(formatted)
            # Add sources from SPARQL rows that carry a URI
            for r in rows[:10]:
                uri = r.get("uri", "")
                name = r.get("name", "")
                if uri and name:
                    sources.append({"title": name, "url": uri})

    # ── Semantic path ────────────────────────────────────────────────────────
    semantic_results = _wiki_search(req.question, req.top_k)
    above_threshold = [r for r in semantic_results if r.get("score", 0) >= req.min_score]

    if above_threshold:
        lines = ["[Beeld & Geluid Wiki — achtergrond]"]
        for r in above_threshold:
            title = r.get("title", "?")
            url = r.get("url", "")
            excerpt = r.get("excerpt", "")
            last_edited = r.get("last_edited", "")
            stale = r.get("staleness_warning", "")
            lines.append(f"\n## {title}")
            if excerpt:
                lines.append(excerpt)
            if stale:
                lines.append(f"Let op: {stale}")
            if url:
                date = last_edited[:10] if last_edited else "?"
                lines.append(f"Bron: {url} (bijgewerkt: {date})")
            if url and title:
                sources.append({"title": title, "url": url})
        context_parts.append("\n".join(lines))

    context = "\n\n---\n\n".join(context_parts)

    # Deduplicate sources by URL
    seen: set[str] = set()
    unique_sources = []
    for s in sources:
        if s["url"] not in seen:
            seen.add(s["url"])
            unique_sources.append(s)

    return {
        "context": context,
        "sources": unique_sources,
        "found": bool(context_parts),
        "sparql_hits": sparql_hits,
        "semantic_hits": len(above_threshold),
    }


@app.post("/search")
def search(req: SearchRequest) -> list[dict]:
    try:
        return _wiki_search(req.query, req.limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/lookup")
def lookup(req: TitleRequest) -> dict | None:
    try:
        return _wiki_lookup(req.title)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/metadata")
def metadata(req: TitleRequest) -> dict | None:
    try:
        return _wiki_metadata(req.title)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/query")
def query(req: QueryRequest) -> list[dict]:
    try:
        return _wiki_query(req.query_name, req.params)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
