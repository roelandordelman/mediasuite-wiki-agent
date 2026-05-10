#!/usr/bin/env python3
"""
REST API wrapper for the Beeld & Geluid Wiki tools.

Exposes the same four tools as the MCP server as plain JSON REST endpoints,
so the media-suite-learn-chatbot can call them without MCP client complexity.

Run:
  python3.12 -m uvicorn api.serve:app --port 8002

Endpoints:
  GET  /health
  POST /search    wiki_search(query, limit)
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

app = FastAPI(title="Beeld & Geluid Wiki API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


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
