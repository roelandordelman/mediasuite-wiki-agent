# mediasuite-wiki-agent

Harvests, indexes, and serves the [Beeld & Geluid Wiki](https://wiki.beeldengeluid.nl) as an MCP server for the CLARIAH Media Suite.

The wiki contains ~24,000 articles about persons, productions, genres, and topics in Dutch audiovisual media history, written by Sound and Vision staff and experts (CC-BY-SA 4.0). This project makes that knowledge queryable by AI agents and connects it to the broader Sound and Vision linked data infrastructure via GTAA.

## Why this exists

### The problem with the wiki

The Beeld & Geluid Wiki is a curated knowledge base that Sound and Vision staff have been building for years. It has detailed biographical articles on thousands of Dutch media personalities, production histories, genre descriptions, and decade overviews. But it has two problems:

1. **It is fragile.** The wiki runs on Sound and Vision infrastructure with no guaranteed continuity and only a handful of active editors. If the server goes down, 24,000 articles of institutional knowledge disappear. This project creates a persistent, independently stored and indexed copy.

2. **It is not machine-readable.** The wiki is a collection of MediaWiki pages. An AI agent cannot query it efficiently, cannot find "all productions in genre X from the 1970s", and cannot connect a wiki article about Rob de Nijs to the Sound and Vision archive items that feature him. This project solves both.

### Why MCP

MCP (Model Context Protocol) is an open standard for connecting AI agents to external tools and data sources. Instead of baking wiki access into a specific chatbot, the wiki agent exposes its capabilities as MCP tools that any compatible AI agent can call.

In practice this means: the Media Suite chatbot (and any future agent in the CLARIAH ecosystem) can call `wiki_search`, `wiki_lookup`, and `wiki_metadata` as tools — the same way a developer calls a function — without knowing anything about Milvus, embeddings, or the MediaWiki API. The wiki agent handles all of that internally.

This also means the wiki agent is composable. It sits alongside other MCP servers (the Media Suite documentation knowledge base, NDE collection tools) and a routing layer decides which tool to call for a given question. A researcher asking "who was Rob de Nijs?" gets wiki biographical context. A researcher asking "how do I search by date in the Media Suite?" gets documentation. The same underlying infrastructure handles both.

### The GTAA connection

GTAA (Gemeenschappelijke Thesaurus Audiovisuele Archieven) is the controlled vocabulary Sound and Vision uses to catalogue its collection. Collection items in the archive are tagged with GTAA URIs for persons, genres, and topics. The wiki predates this linked data layer and has no GTAA links — but its article titles largely match GTAA preferred labels.

During harvest, each wiki article is matched to a GTAA concept URI via the Sound and Vision SPARQL endpoint. Once that link exists, a researcher asking about Rob de Nijs can get:
- Wiki biographical context (from this agent, via `wiki_lookup`)
- Archive items featuring Rob de Nijs (via the GTAA URI → collection SPARQL)

This connection requires no changes to the collection metadata. It is the bridge between the wiki and the rest of the Media Suite knowledge graph.

## What is built (Phase 1 — complete)

The full pipeline from raw wiki to running MCP server has been implemented and tested.

### Harvest pipeline

```
wiki.beeldengeluid.nl/api.php
        │  MediaWiki Action API
        ▼
harvest/harvest_articles.py   →  data/articles/{pageid}.json
        │  raw wikitext + categories + timestamp
        ▼
harvest/clean_wikitext.py     →  data/cleaned/{pageid}.json
        │  plain text, infobox removed
        ▼
harvest/extract_structured.py →  data/structured/{pageid}.json
        │  typed records: persoon / productie / genre / redirect / …
        ▼
harvest/link_gtaa.py          →  data/structured/{pageid}.json  (updated)
        │  gtaa_uri + gtaa_match_confidence added via SPARQL
        ▼
index/chunk.py                →  data/chunks/{pageid}.json
        │  ~800-char sliding window; chunk 0 = structured infobox summary
        ▼
index/embed.py                →  data/milvus_wiki.db
           multilingual-e5-large-instruct embeddings → Milvus
```

**24,104 articles** harvested, cleaned, extracted, linked, chunked, and embedded.

### MCP server

```bash
python3.12 mcp/server.py
```

Three tools:

**`wiki_search(query, limit=5)`** — semantic search over the Milvus index. Takes a natural language question in Dutch or English and returns ranked excerpts with source URL and last-edited timestamp. Use for open-ended questions: "who were the key presenters of Dutch public television in the 1980s?", "what is a praatprogramma?".

**`wiki_lookup(title)`** — exact or near-exact lookup by person name, production title, or topic. Returns a structured summary built from the infobox (birth/death dates, functions, period active, collaborators, known for) plus lead text from the article. This is the primary tool when a specific name is known — e.g. when a researcher clicks on a search result and the interface wants to surface background context. Falls back to semantic search if no exact match is found.

**`wiki_metadata(title)`** — returns article metadata only: page ID, URL, categories, last-edited timestamp, GTAA URI, article type. Useful for resolving a name to a GTAA URI, or checking article freshness before presenting information.

Every tool response includes the article URL, last-edited timestamp, a source note, and a staleness warning for articles not edited in more than two years.

### Infrastructure

The Phase 1 setup is intentionally simple and fully local:

- **Milvus Lite** — the index lives in `data/milvus_wiki.db`, a single local file. No Docker, no server. Uses the same `pymilvus` SDK as a production Milvus cluster — swapping to a real instance is one environment variable: `MILVUS_URI=http://milvus:19530`.
- **sentence-transformers** — embeddings computed locally with `intfloat/multilingual-e5-large-instruct`. The embedder is behind a one-function interface; replacing it with an HTTP call to the mediasuite-agent embedding API requires changing one class.

## Running

### Prerequisites

```bash
# Harvest pipeline (Python 3.9+)
pip install -r requirements.txt

# MCP server (Python 3.10+ required by the MCP SDK)
python3.12 -m pip install -r requirements.txt
```

### Full pipeline (first run)

```bash
python3 harvest/harvest_articles.py     # ~30 min, fetches 24k articles
python3 harvest/clean_wikitext.py       # ~10 min
python3 harvest/extract_structured.py  # ~10 min
python3 harvest/link_gtaa.py           # ~45 min, SPARQL lookups
python3 index/chunk.py                 # fast
python3 index/embed.py                 # slow on CPU — run overnight
```

All steps are resumable: re-running skips already-processed files.

### MCP server

```bash
python3.12 mcp/server.py              # start server (stdio transport)
python3.12 mcp/server.py --test       # smoke test against the live index
```

To register with a Claude Code session or any MCP client, point at `python3.12 mcp/server.py` as the server command with the repo root as the working directory.

## Data layout

```
data/
├── pagelist.json        # Enumerated page IDs and titles (harvest checkpoint)
├── articles/            # Raw JSON per article — gitignored
├── cleaned/             # Plain text per article — gitignored
├── structured/          # Infobox records + GTAA URIs — gitignored
├── chunks/              # Chunk lists per article — gitignored
└── milvus_wiki.db       # Milvus Lite index — gitignored
```

## What is next (Phase 2 — structured store)

Phase 1 answers questions well when the answer lives in the narrative text of an article. But the wiki's infobox data is highly structured, and some questions are better answered by a database than by a vector search:

- "Which persons were active in Dutch television between 1960 and 1975?"
- "Which productions are in the genre 'praatprogramma' and aired on VARA?"
- "Who collaborated with Mies Bouwman?"

Semantic search handles these poorly. A vector index does not know that `period_start=1960` and `period_end=1975` mean "active in this period" — it just sees text.

Phase 2 loads the structured records from `data/structured/` into a SQLite database with typed tables (`persons`, `productions`, `articles`, `links`) and adds a fourth MCP tool, `wiki_query`, that runs named query templates against it. The routing layer can then decide: open-ended question → `wiki_search`, specific relational question → `wiki_query`, known name → `wiki_lookup`.

The SQLite store is also the foundation for Phase 3, where it gets converted to RDF and aligned with the NDE Termennetwerk, making the wiki agent a node in the Dutch heritage linked data network.

## Status

| Phase | Step | Script | Status |
|---|---|---|---|
| 1 | Harvest | `harvest/harvest_articles.py` | Done — 24,104 articles |
| 1 | Clean | `harvest/clean_wikitext.py` | Done |
| 1 | Extract | `harvest/extract_structured.py` | Done |
| 1 | GTAA link | `harvest/link_gtaa.py` | Done |
| 1 | Chunk + embed | `index/chunk.py`, `index/embed.py` | Done |
| 1 | MCP server | `mcp/server.py` | Done |
| 1 | Sync job | `index/sync.py` | Pending |
| 2 | SQLite store | `index/structured_store.py` | Pending |
| 2 | Named queries | — | Pending |
| 2 | `wiki_query` tool | `mcp/server.py` | Pending |
| 3 | RDF export + SPARQL | — | Planned |

## Relationship to other repos

- **mediasuite-knowledge-base** — documentation KB (how to use the Media Suite); separate Milvus collection, separate MCP tools; do not mix with wiki content
- **media-suite-learn-chatbot** — the chatbot that calls both the documentation KB MCP and this wiki MCP
- **mediasuite-agent** — the main agent layer (embedding API, Milvus, orchestration); this MCP server registers with it
