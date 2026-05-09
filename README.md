# mediasuite-wiki-agent

Harvests, indexes, and serves the [Beeld & Geluid Wiki](https://wiki.beeldengeluid.nl) as an MCP tool for the CLARIAH Media Suite.

The wiki contains ~24,000 articles about persons, productions, genres, and topics in Dutch audiovisual media history, written by Sound and Vision staff and experts (CC-BY-SA). This project makes that content queryable by AI agents via semantic search and structured lookups, and links it to the GTAA controlled vocabulary so wiki articles connect to the broader Media Suite knowledge graph.

## Prerequisites

- Python 3.9+
- `pip install -r requirements.txt`
- Milvus instance (for the embedding index — Phase 1 embedding step)

## Pipeline

The harvest pipeline runs in four sequential steps. Each step is resumable: re-running skips files that already exist.

### Step 1 — Harvest

Fetch all articles from the MediaWiki API. Saves one JSON file per article to `data/articles/`.

```bash
python3 harvest/harvest_articles.py
```

Options: `--batch-size 50` (default), `--delay 0.5` (seconds between requests), `--output-dir data/articles`, `--pagelist data/pagelist.json`.

The pagelist is saved as a checkpoint — re-runs skip the enumeration phase.

### Step 2 — Clean

Strip MediaWiki markup from raw wikitext → plain text. Saves to `data/cleaned/`.

```bash
python3 harvest/clean_wikitext.py
```

Each output file includes `text` (plain text with infobox removed), `is_redirect`, `has_infobox`, and `infobox_type`.

### Step 3 — Extract structured data

Parse infobox templates → typed structured records. Saves to `data/structured/`.

```bash
python3 harvest/extract_structured.py
```

Extracts:
- **Persoon**: `naam`, `birth_date`, `birth_place`, `death_date`, `death_place`, `functions`, `period_start`, `period_end`, `collaborators`, `known_for`
- **Productie**: `genre`, `medium`, `period_start`, `period_end`, `persons` (from the Makers section)

Article type is detected from the infobox template name, falling back to categories.

### Step 4 — GTAA entity linking

Match each article to a GTAA concept URI via the Sound and Vision SPARQL endpoint. Updates `data/structured/` files in-place, adding `gtaa_uri` and `gtaa_match_confidence`.

```bash
python3 harvest/link_gtaa.py
```

Options: `--workers 5` (parallel SPARQL requests), `--types persoon productie genre`.

Person names are automatically inverted to GTAA format (`"Rob de Nijs"` → `"Nijs, Rob de"`). Confidence values: `exact` (prefLabel match), `alias` (altLabel match), `none`.

**SPARQL endpoint:** `https://cat.apis.beeldengeluid.nl/sparql`

### Step 5 — Embed (coming next)

Chunk cleaned articles and embed into Milvus collection `mediasuite_wiki`.

```bash
python3 index/chunk.py
python3 index/embed.py
```

### Step 6 — MCP server (coming next)

```bash
python3 mcp/server.py
```

Exposes `wiki_search`, `wiki_lookup`, and `wiki_metadata` tools.

## Data layout

```
data/
├── pagelist.json        # Checkpoint: all enumerated page IDs and titles
├── articles/            # Raw JSON per article (gitignored)
├── cleaned/             # Plain text per article (gitignored)
└── structured/          # Infobox records + GTAA URIs (gitignored)
```

## GTAA connection

Once an article has a `gtaa_uri`, it connects to the rest of the Media Suite linked data infrastructure. A researcher asking about a person can get wiki biographical context alongside collection items from the Sound and Vision archive — via the shared GTAA URI — without any changes to the collection metadata.

## Status

| Step | Script | Status |
|---|---|---|
| Harvest | `harvest/harvest_articles.py` | Done — 24,104 articles |
| Clean | `harvest/clean_wikitext.py` | Done |
| Extract | `harvest/extract_structured.py` | Done |
| GTAA link | `harvest/link_gtaa.py` | Done |
| Chunk + embed | `index/chunk.py`, `index/embed.py` | Pending |
| MCP server | `mcp/server.py` | Pending |
| Sync job | `index/sync.py` | Pending |

## Relationship to other repos

- **mediasuite-knowledge-base** — documentation KB (how to use Media Suite); separate Milvus collection, do not mix
- **media-suite-learn-chatbot** — calls both the documentation KB MCP and this wiki MCP
- **mediasuite-agent** — main agent layer; this MCP server registers with it
