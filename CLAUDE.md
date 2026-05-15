# mediasuite-wiki-agent — Claude Code Context

## What this repository is

This repository builds and maintains the **B&G Wiki Agent**: a harvester,
index, and MCP (Model Context Protocol) server that makes the content of the
Beeld & Geluid Wiki (wiki.beeldengeluid.nl) queryable by AI agents and
integratable into the CLARIAH Media Suite infrastructure.

The B&G Wiki contains 21,339 articles about persons, productions, genres,
decades, and topics related to Dutch audiovisual media history, written by
Sound and Vision staff and experts. It is CC-BY-SA licensed. The wiki is
lightly maintained (approximately 5 active users in 30 days as of May 2026)
and runs on Sound and Vision infrastructure with no guaranteed continuity.
This project harvests and indexes the content as a resilient, independently
queryable resource.

## Why this exists

Two problems this project solves:

1. **Preservation risk.** If the wiki.beeldengeluid.nl server goes down,
   21,000 articles of curated knowledge about Dutch media history disappear.
   This project creates a persistent, independently stored copy.

2. **Integration into the Media Suite agent ecosystem.** The wiki contains
   rich contextual knowledge that is useful alongside Media Suite search:
   when a researcher searches for a person, production, or topic, the wiki
   agent can surface relevant background context. This project exposes that
   knowledge as an MCP tool callable by the Media Suite chatbot and
   potentially by the Media Suite interface itself.

## Architecture

```
mediasuite-wiki-agent/
├── harvest/
│   ├── harvest_articles.py     # MediaWiki API → JSON per article
│   ├── clean_wikitext.py       # Strip MediaWiki markup → plain text
│   ├── extract_structured.py   # Infobox fields → structured records
│   ├── link_gtaa.py            # Match titles/names to GTAA URIs via SPARQL
│   ├── harvest_images.py       # List/verify files on Wikimedia Commons (planned)
│   └── run_harvest.sh          # Full harvest pipeline runner (planned)
├── index/
│   ├── chunk.py                # Split articles into retrieval chunks
│   ├── embed.py                # Embed chunks → Milvus collection "wiki"
│   ├── structured_store.py     # Infobox data → SQLite (later RDF)
│   └── sync.py                 # Incremental sync via RecentChanges API
├── mcp/
│   ├── server.py               # MCP server entry point
│   ├── tools/
│   │   ├── wiki_search.py      # Semantic search over Milvus wiki index
│   │   ├── wiki_query.py       # Structured queries over SQLite store
│   │   ├── wiki_lookup.py      # Exact title lookup by person/production name
│   │   └── wiki_metadata.py    # Last edited, categories, GTAA URI
│   └── prompts/
│       └── system.md           # System prompt for wiki agent responses
├── data/
│   ├── articles/               # Raw JSON per article (gitignored)
│   ├── cleaned/                # Plain text per article (gitignored)
│   ├── structured/             # Extracted infobox records as JSON (gitignored)
│   ├── wiki.db                 # SQLite structured store (gitignored)
│   └── wiki_dump.xml.gz        # Full XML backup when available (gitignored)
├── evaluation/
│   ├── test_questions.json     # Structured test questions for retrieval quality
│   └── evaluate.py             # Hit@10, MRR evaluation runner
└── docs/
    └── decisions.md            # Architecture decisions and rationale
```

## The source — wiki.beeldengeluid.nl

**Base URL:** `https://wiki.beeldengeluid.nl`
**API endpoint:** `https://wiki.beeldengeluid.nl/api.php`
**Protocol:** MediaWiki Action API (public, no authentication required)
**License:** CC-BY-SA 4.0
**Language:** Dutch (nl)
**Scale:** ~21,339 content pages, ~43,451 files

**Five navigation categories:**
- `Categorie:Personen` — persons active in Dutch media (presenters, actors,
  directors, musicians, journalists)
- `Categorie:Producties` — productions (TV series, radio programmes, films)
- `Categorie:Genres` — genre descriptions (talk show, documentary, drama, etc.)
- `Categorie:Onderwerpen` — topics (media history themes)
- `Decennia` — decade-based overviews (1950s, 1960s, etc.)

**Article structure:** Articles use MediaWiki markup with infobox templates
(`{{ Infobox Persoon }}`, `{{ Infobox Productie }}`), chronological narrative
text, internal links `[[Article Title]]`, and category tags at the bottom.

**Key API calls:**

```python
# Enumerate all articles (paginated, use apcontinue)
GET api.php?action=query&list=allpages&aplimit=500&apnamespace=0&format=json

# Get article content + categories + last edit timestamp
GET api.php?action=query&titles={title}&prop=revisions|categories
    &rvprop=content|timestamp&rvslots=main&format=json

# Get recent changes (for incremental sync)
GET api.php?action=query&list=recentchanges&rcprop=title|timestamp|comment
    &rclimit=500&rcnamespace=0&rctype=edit|new&format=json

# Search (full-text, returns snippets)
GET api.php?action=query&list=search&srsearch={query}
    &srlimit=10&srprop=snippet&format=json
```

**Wikitext cleaning:** Use `mwparserfromhell` to strip markup.
Key steps: parse wikitext → extract plain text → remove infobox templates
(keep structured fields separately) → normalise whitespace.

## The index — Milvus collection "wiki"

The wiki articles are indexed in a **separate Milvus collection** from the
Media Suite documentation knowledge base (`mediasuite_docs`). This is
intentional: mixing them would create retrieval noise. The routing layer
decides which collection to query based on question type.

**Collection name:** `mediasuite_wiki`

**Schema per chunk:**
```python
{
    "chunk_id": str,           # "{pageid}_{chunk_index}"
    "pageid": int,             # MediaWiki page ID
    "title": str,              # Article title (canonical)
    "url": str,                # https://wiki.beeldengeluid.nl/index.php/{title}
    "categories": list[str],   # e.g. ["Personen", "Acteur", "Zanger"]
    "last_edited": str,        # ISO timestamp of last revision
    "chunk_text": str,         # Plain text chunk (~800 chars)
    "chunk_index": int,        # Position within article
    "is_infobox": bool,        # True for the structured infobox chunk
    "gtaa_uri": str | None,    # e.g. "http://data.beeldengeluid.nl/gtaa/123456"
    "embedding": list[float],  # multilingual-e5-large-instruct vector
}
```

**Chunking strategy:** ~800 characters per chunk with overlap. For articles
with infobox templates, extract the infobox fields as a separate first chunk
with structured key-value text (e.g. "Naam: Robert de Nijs. Geboren: 26
december 1942. Functies: Acteur, Zanger.") before chunking the narrative.
This ensures person/production facts are retrievable even without matching
the narrative.

**Embedding model:** `multilingual-e5-large-instruct` — same model as the
rest of the Media Suite agent ecosystem. Dutch and English in the same
vector space.

## The MCP tools

Four tools exposed by the MCP server:

### `wiki_search(query: str, limit: int = 5) -> list[dict]`
Semantic search over the Milvus wiki index. Used for open-ended questions:
"who presented the show X", "what genre is Y", "what happened in Dutch
television in the 1970s". Returns ranked chunks with source URL and
last-edited timestamp.

### `wiki_query(query_name: str, params: dict) -> list[dict]`
Named structured queries over the SQLite store. Used for precise relational
questions. See **Named structured queries** below for the catalogue.

### `wiki_lookup(title: str) -> dict | None`
Exact or near-exact title lookup. Used when a specific person or production
name is known. First tries exact match against the structured store; falls
back to MediaWiki search API for disambiguation. Returns the lead chunk plus
infobox fields and GTAA URI if available. This is the tool for the Media
Suite interface integration: when a user searches for a person name in the
Media Suite, `wiki_lookup` can be called to surface a wiki summary alongside
search results.

### `wiki_metadata(title: str) -> dict`
Returns article metadata without content: last edited, categories, page ID,
URL, GTAA URI, edit count. Useful for checking whether wiki information is
current before presenting it to a researcher.

## Dual retrieval: semantic search + structured queries

The wiki supports two complementary retrieval paths, mirroring the dual-path
architecture of the Media Suite documentation knowledge base. The routing
layer decides which path(s) to use based on question type.

**Semantic search (Milvus)** handles:
- Open-ended, thematic, exploratory questions
- Questions with vocabulary variation or partial information
- "Tell me about...", "What was the significance of...", "Who were the key
  figures in..."

**Structured queries (SQLite)** handle:
- Precise relational, factual questions
- Questions about specific attributes (birth year, period active, genre,
  collaborators)
- "Which persons were born in Amsterdam?", "Which productions ran between
  1965 and 1975?", "Who collaborated with Rob de Nijs?"

For complex questions, both paths run and the LLM synthesises from both
result sets.

### Named structured queries

The SQLite store holds one row per article with extracted infobox fields.
Named query templates (analogous to the SPARQL catalogue in the documentation
KB) cover the most common structured question types:

```python
NAMED_QUERIES = {
    # Persons
    "persons_by_function": "SELECT * FROM persons WHERE functions LIKE ?",
    "persons_by_birth_place": "SELECT * FROM persons WHERE birth_place = ?",
    "persons_active_in_period": "SELECT * FROM persons WHERE period_start <= ? AND period_end >= ?",
    "persons_collaborated_with": "SELECT * FROM persons WHERE collaborators LIKE ?",

    # Productions
    "productions_by_genre": "SELECT * FROM productions WHERE genre = ?",
    "productions_by_period": "SELECT * FROM productions WHERE period_start <= ? AND period_end >= ?",
    "productions_by_broadcaster": "SELECT * FROM productions WHERE broadcaster = ?",
    "productions_featuring_person": "SELECT * FROM productions WHERE persons LIKE ?",

    # Cross-entity
    "wiki_article_for_gtaa": "SELECT * FROM articles WHERE gtaa_uri = ?",
    "related_articles": "SELECT * FROM articles WHERE title IN (SELECT related FROM links WHERE source = ?)",
}
```

### SQLite schema

```sql
CREATE TABLE articles (
    pageid      INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    categories  TEXT,           -- JSON array
    last_edited TEXT,           -- ISO timestamp
    gtaa_uri    TEXT,           -- GTAA concept URI if matched
    article_type TEXT           -- 'persoon' | 'productie' | 'genre' | 'onderwerp' | 'other'
);

CREATE TABLE persons (
    pageid          INTEGER PRIMARY KEY REFERENCES articles(pageid),
    naam            TEXT,
    birth_date      TEXT,
    birth_place     TEXT,
    death_date      TEXT,
    death_place     TEXT,
    functions       TEXT,       -- JSON array: ["Acteur", "Zanger"]
    period_start    INTEGER,
    period_end      INTEGER,
    collaborators   TEXT,       -- JSON array of wiki titles
    known_for       TEXT        -- JSON array of production titles
);

CREATE TABLE productions (
    pageid          INTEGER PRIMARY KEY REFERENCES articles(pageid),
    genre           TEXT,
    broadcaster     TEXT,
    period_start    INTEGER,
    period_end      INTEGER,
    medium          TEXT,       -- 'Televisie' | 'Radio' | 'Film'
    persons         TEXT        -- JSON array of person names (presentators, makers)
);

CREATE TABLE links (
    source          INTEGER REFERENCES articles(pageid),
    target_title    TEXT        -- raw [[link target]] from wikitext
);
```

## GTAA entity linking

The GTAA (Gemeenschappelijke Thesaurus Audiovisuele Archieven) is the shared
controlled vocabulary used by Sound and Vision and other Dutch AV archives for
cataloguing. It is published as linked open data at data.beeldengeluid.nl
under ODbL license, queryable via SPARQL.

**The wiki does not link to GTAA URIs directly** — the wiki predates the
linked data era at Sound and Vision. However, the vocabulary alignment is
strong: wiki categories (Personen, Genre, Onderwerpen) correspond to GTAA
concept schemes, and wiki article titles for persons and productions largely
match GTAA preferred labels.

**Entity linking at harvest time** resolves this. During harvest, for each
article the linker:

1. Determines the article type from categories and infobox template.
2. Inverts person names to GTAA form (`"Rob de Nijs"` → `"Nijs, Rob de"`).
3. Queries the GTAA SPARQL endpoint for a matching `skos:prefLabel`, falling
   back to `skos:altLabel`:

```sparql
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>

SELECT ?concept WHERE {
  ?concept skos:prefLabel ?l ;
           skos:inScheme <http://data.beeldengeluid.nl/gtaa/Persoonsnamen> .
  FILTER(STR(?l) = "Nijs, Rob de")
} LIMIT 1
```

4. If a match is found, stores the GTAA URI in both the Milvus chunk metadata
   and the SQLite articles table.

**GTAA SPARQL endpoint:** `https://cat.apis.beeldengeluid.nl/sparql`

**Verified scheme URIs (as of May 2026):**

| Article type | GTAA scheme URI | Entries |
|---|---|---|
| persoon | `http://data.beeldengeluid.nl/gtaa/Persoonsnamen` | 385,822 |
| productie | `http://data.beeldengeluid.nl/gtaa/Programmatitels` | — |
| genre | `http://data.beeldengeluid.nl/gtaa/Genre` | 167 |
| onderwerp | `http://data.beeldengeluid.nl/gtaa/Onderwerpen` + `OnderwerpenBenG` | 4,136 + 4,770 |
| omroep | `http://data.beeldengeluid.nl/gtaa/Namen` | 34,169 |

**Person name inversion:** `Persoonsnamen` stores labels in inverted form
(`"Surname, Firstname Tussenvoegsel"`). The linker inverts wiki article titles
before querying: `"Rob de Nijs"` → `"Nijs, Rob de"`, `"Paul van Vliet"` →
`"Vliet, Paul van"`. It also tries the full name from the infobox `naam` field
as a fallback.

**Why this matters:** Once a wiki article is linked to a GTAA URI, it connects
to the rest of the Media Suite linked data infrastructure. The collection
SPARQL endpoint at data.beeldengeluid.nl uses GTAA URIs for persons and topics
in collection metadata. A researcher asking about Rob de Nijs can get:
- Wiki biographical context (from this agent)
- Collection items featuring Rob de Nijs (via GTAA URI → collection SPARQL)

This is the connection between the wiki agent and the broader Media Suite
knowledge graph — without requiring any changes to the collection metadata.

**Matching confidence:** Not all wiki articles will have GTAA matches. Track
match rate during harvest and store a `gtaa_match_confidence` field:
- `exact` — prefLabel match
- `alias` — altLabel match
- `none` — no match found

Do not create false positives. If uncertain, leave `gtaa_uri` as null.

## Routing guidance for the calling agent

The wiki agent should be queried when:
- The question contains a specific person name, production title, or genre
- The question is about Dutch media history context
- A researcher is looking at a search result and wants background on a person
  or production

The wiki agent should NOT be queried when:
- The question is about how to use the Media Suite (→ documentation KB)
- The question is about collection coverage or access conditions (→ NDE tools)
- The question requires current or recent information (the wiki is not
  a news source)

## Provenance and uncertainty

Every response from the wiki agent must include:
- The article URL (`https://wiki.beeldengeluid.nl/index.php/{title}`)
- The last-edited timestamp
- A note that the wiki is maintained by Sound and Vision staff and volunteers
  but is not actively managed and may contain outdated information

Articles not edited in more than 2 years should be flagged as potentially
outdated when the information is time-sensitive.

## The sync job

The wiki is updated occasionally (5 active users in last 30 days, May 2026).
The sync job polls the RecentChanges API daily, identifies changed articles,
re-fetches and re-embeds only those articles, and updates the Milvus index.

If the wiki.beeldengeluid.nl server becomes unreachable, the sync job logs
the failure and continues serving from the last indexed state. The index
does not become unavailable when the source goes down.

## Images

The wiki's ~43,000 image files are largely already mirrored to Wikimedia
Commons under `Category:Media from Beeld en Geluid Wiki` (3,129 files
confirmed as of May 2026). The harvest pipeline does not need to copy images;
it can reference Wikimedia Commons URLs where available.

For images not on Wikimedia Commons: check availability via the MediaWiki
file API, note the URL in the article metadata, but do not download unless
explicitly requested.

## Preservation backup

A full XML dump of all article text should be requested from Sound and Vision
infrastructure team and stored in `data/wiki_dump.xml.gz` (gitignored due to
size). This is the preservation copy independent of the Milvus index.

Until the XML dump is available, the harvest pipeline serves as the backup:
run `harvest/harvest_articles.py` to completion and store the resulting JSON
files in `data/articles/`.

## Relationship to other Media Suite agent repos

- **mediasuite-knowledge-base** — documentation KB (how to use Media Suite);
  separate Milvus collection, separate MCP tools; do not mix with wiki content
- **media-suite-learn-chatbot** — the chatbot application; calls both the
  documentation KB MCP and this wiki MCP as needed
- **mediasuite-agent** — the main agent layer (embedding API, Milvus,
  orchestration); the wiki agent MCP server registers with this layer

## Key design decisions

**Separate collection, not mixed index.** The wiki is reference knowledge
(who/what is X); the documentation KB is procedural knowledge (how do I Y).
Mixing them creates retrieval noise in both directions. Separate collections
with routing logic is the right architecture.

**Dual retrieval: semantic + structured.** The wiki's infobox data is
structured enough to support precise relational queries that semantic search
handles poorly. Named query templates over SQLite mirror the SPARQL query
catalogue pattern from the documentation knowledge base. Phase 3 upgrades
the SQLite store to RDF/SPARQL for full linked data alignment.

**GTAA entity linking at harvest time, not query time.** Matching wiki
article titles to GTAA URIs during harvest is cheap and reliable. Doing it
at query time would add latency and complexity to every lookup. The GTAA
match is stored once and reused; it is updated during sync when articles
change.

**Live sync over snapshot.** While a full backup is essential for
preservation, the operational index should be kept current via the
RecentChanges API sync. This respects the ongoing editorial work of the
small community still maintaining the wiki.

**Dutch content, multilingual retrieval.** All wiki content is in Dutch.
The multilingual-e5-large-instruct model handles Dutch-language content
correctly in the same vector space as English queries. The MCP response
should note that source content is in Dutch and that the LLM synthesis
is translating/summarising.

**Degrade gracefully.** If the MediaWiki instance goes down, the MCP server
continues operating from the Milvus index and SQLite store. If Milvus is
unavailable, structured queries still work. If both are unavailable, the
MCP tool returns a clear error rather than a silent failure.

## Phased implementation

**Phase 1 — Harvest + semantic index (start here)**
- Harvest all articles via MediaWiki API
- Clean wikitext → plain text
- Extract infobox fields → structured records
- **GTAA entity linking** via SPARQL (done at harvest time alongside extraction)
- Chunk + embed → Milvus `mediasuite_wiki` collection
- Basic MCP tools: `wiki_search`, `wiki_lookup`, `wiki_metadata`
- JSON backup in `data/articles/`

**Phase 2 — Structured store**
- Load infobox records → SQLite schema above
- Named structured query templates
- Add `wiki_query` MCP tool
- Routing layer decides semantic vs structured vs both

**Phase 3 — RDF/SPARQL + full linked data alignment**
- Convert SQLite to RDF using SKOS/Schema.org
- Expose SPARQL endpoint
- Align with NDE Termennetwerk
- The wiki agent becomes a node in the Dutch heritage linked data network

## Language

- Code: English
- Wiki source content: Dutch
- MCP tool docstrings and prompts: English
- Evaluation test questions: Dutch and English (the agent serves both)

## Status (as of May 2026)

**Phase 1 — Harvest + semantic index**
- [x] Harvest pipeline (`harvest_articles.py`) — 24,104 articles harvested
- [x] Wikitext cleaning (`clean_wikitext.py`)
- [x] Infobox extraction (`extract_structured.py`)
- [x] GTAA entity linker (`link_gtaa.py`) — run after extraction
- [x] Milvus index (`chunk.py`, `embed.py`)
- [x] MCP server with `wiki_search`, `wiki_lookup`, `wiki_metadata` (`mcp/server.py`)
- [ ] Sync job (`sync.py`)
- [x] JSON backup in `data/articles/`

**Phase 2 — Knowledge graph + structured queries**
- [x] RDF export (`index/build_rdf.py`) — 302,997 triples
- [x] Fuseki loader (`index/load_fuseki.py`)
- [x] Named SPARQL queries (`mcp/sparql_queries.py`) — 8 query templates
- [x] `wiki_query` MCP tool (`mcp/server.py`)

**Chatbot integration**
- [x] REST API wrapper (`api/serve.py`) — exposes MCP tools as JSON REST endpoints
- [x] Integrated into `media-suite-learn-chatbot` as a third retrieval path

**Phase 3 — NDE Termennetwerk alignment (deferred)**
- Planned after NISV infrastructure integration
- GTAA links are already in the graph; making them discoverable via Termennetwerk
  is an institutional step, not a technical one.

**Pending from Sound and Vision**
- [ ] XML dump from infrastructure team (preservation backup)

## Running the REST API

```bash
# Start REST API (separate from the MCP server)
python3.12 -m uvicorn api.serve:app --port 8002

# The chatbot expects the wiki API at http://localhost:8002
# configured via wiki_api.url in media-suite-learn-chatbot/config.yaml
```

## Roadmap
The shared project roadmap is at `../mediasuite-knowledge-base/docs/roadmap.md`.
Before starting significant work, check current priorities there.
