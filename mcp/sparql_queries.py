"""
Named SPARQL query templates for the Beeld & Geluid Wiki knowledge graph.

All queries operate on the named graph <https://wiki.beeldengeluid.nl/graph>
in the Fuseki dataset. Parameters are injected via .format(**kwargs).

Same structure as mediasuite-knowledge-base/pipelines/graph/sparql_queries.py.
"""

from __future__ import annotations

import os

import requests

FUSEKI_URL  = os.getenv("FUSEKI_URL", "http://localhost:3030")
DATASET     = os.getenv("FUSEKI_DATASET", "wiki")
SPARQL_URL  = f"{FUSEKI_URL}/{DATASET}/sparql"
GRAPH_URI   = "https://wiki.beeldengeluid.nl/graph"
AUTH        = (os.getenv("FUSEKI_USER", "admin"), os.getenv("FUSEKI_PASSWORD", "admin"))

PREFIXES = """
PREFIX schema: <https://schema.org/>
PREFIX skos:   <http://www.w3.org/2004/02/skos/core#>
PREFIX dcterms:<http://purl.org/dc/terms/>
PREFIX beng:   <https://wiki.beeldengeluid.nl/vocab#>
PREFIX xsd:    <http://www.w3.org/2001/XMLSchema#>
"""

GRAPH = f"GRAPH <{GRAPH_URI}>"

# ── Query templates ────────────────────────────────────────────────────────────

QUERIES: dict[str, str] = {

    # Who are the Dutch TV presenters / directors / etc.?
    # param: function (e.g. "presentator", "regisseur")
    "persons_by_function": PREFIXES + """
SELECT DISTINCT ?uri ?name ?function WHERE {{
  {graph} {{
    ?uri a schema:Person ;
         schema:name ?name ;
         schema:jobTitle ?function .
    FILTER(LCASE(STR(?function)) = LCASE("{function}"))
  }}
}} ORDER BY ?name
""",

    # Who was active in Dutch media between year X and year Y?
    # params: start_year (int), end_year (int)
    "persons_active_in_period": PREFIXES + """
SELECT DISTINCT ?uri ?name ?start ?end WHERE {{
  {graph} {{
    ?uri a schema:Person ;
         schema:name ?name ;
         beng:periodStart ?start .
    OPTIONAL {{ ?uri beng:periodEnd ?end }}
    FILTER(?start <= {end_year} && (!BOUND(?end) || ?end >= {start_year}))
  }}
}} ORDER BY ?start ?name
""",

    # Who collaborated with person X?
    # param: person_name (exact wiki article title)
    "persons_collaborated_with": PREFIXES + """
SELECT DISTINCT ?uri ?name WHERE {{
  {graph} {{
    <{person_uri}> schema:colleague ?uri .
    ?uri schema:name ?name .
  }}
}} ORDER BY ?name
""",

    # Which productions are in genre X?
    # param: genre (e.g. "documentaire", "praatprogramma")
    "productions_by_genre": PREFIXES + """
SELECT DISTINCT ?uri ?name ?genre ?start ?end WHERE {{
  {graph} {{
    ?uri a schema:CreativeWork ;
         schema:name ?name ;
         schema:genre ?genre .
    FILTER(LCASE(STR(?genre)) = LCASE("{genre}"))
    OPTIONAL {{ ?uri beng:periodStart ?start }}
    OPTIONAL {{ ?uri beng:periodEnd ?end }}
  }}
}} ORDER BY ?start ?name
""",

    # Which productions aired between year X and year Y?
    # params: start_year (int), end_year (int)
    "productions_in_period": PREFIXES + """
SELECT DISTINCT ?uri ?name ?start ?end ?medium WHERE {{
  {graph} {{
    ?uri a schema:CreativeWork ;
         schema:name ?name ;
         beng:periodStart ?start .
    OPTIONAL {{ ?uri beng:periodEnd ?end }}
    OPTIONAL {{ ?uri beng:medium ?medium }}
    FILTER(?start <= {end_year} && (!BOUND(?end) || ?end >= {start_year}))
  }}
}} ORDER BY ?start ?name
""",

    # Which productions feature person X?
    # param: person_uri (full wiki URL of the person)
    "productions_featuring_person": PREFIXES + """
SELECT DISTINCT ?uri ?name ?genre ?start ?end WHERE {{
  {graph} {{
    ?uri a schema:CreativeWork ;
         schema:name ?name ;
         schema:contributor <{person_uri}> .
    OPTIONAL {{ ?uri schema:genre ?genre }}
    OPTIONAL {{ ?uri beng:periodStart ?start }}
    OPTIONAL {{ ?uri beng:periodEnd ?end }}
  }}
}} ORDER BY ?start ?name
""",

    # Given a GTAA URI, find the wiki article(s) linked to it
    # param: gtaa_uri
    "article_for_gtaa_uri": PREFIXES + """
SELECT DISTINCT ?uri ?name ?type WHERE {{
  {graph} {{
    ?uri skos:exactMatch <{gtaa_uri}> ;
         schema:name ?name ;
         a ?type .
  }}
}}
""",

    # What broadcasters (omroepen) are in the graph?
    "all_broadcasters": PREFIXES + """
SELECT DISTINCT ?uri ?name ?gtaa WHERE {{
  {graph} {{
    ?uri a schema:Organization ;
         schema:name ?name .
    OPTIONAL {{ ?uri skos:exactMatch ?gtaa }}
  }}
}} ORDER BY ?name
""",

    # Person summary: all structured fields for one person
    # param: person_uri
    "person_summary": PREFIXES + """
SELECT ?pred ?obj WHERE {{
  {graph} {{
    <{person_uri}> ?pred ?obj .
  }}
}}
""",

    # Production summary: all structured fields for one production
    # param: production_uri
    "production_summary": PREFIXES + """
SELECT ?pred ?obj WHERE {{
  {graph} {{
    <{production_uri}> ?pred ?obj .
  }}
}}
""",

    # Which productions are for TV, Radio, or Film?
    # param: medium (e.g. "Televisie", "Radio", "Film")
    "productions_by_medium": PREFIXES + """
SELECT DISTINCT ?uri ?name ?medium ?genre ?start ?end WHERE {{
  {graph} {{
    ?uri a schema:CreativeWork ;
         schema:name ?name ;
         beng:medium ?medium .
    FILTER(LCASE(STR(?medium)) = LCASE("{medium}"))
    OPTIONAL {{ ?uri schema:genre ?genre }}
    OPTIONAL {{ ?uri beng:periodStart ?start }}
    OPTIONAL {{ ?uri beng:periodEnd ?end }}
  }}
}} ORDER BY ?start ?name
""",

    # Find all persons in a wiki category (e.g. "Acteur", "Presentator", "Zanger")
    # param: category (e.g. "Acteur" matches dcterms:subject "Acteur")
    "persons_by_category": PREFIXES + """
SELECT DISTINCT ?uri ?name ?cat WHERE {{
  {graph} {{
    ?uri a schema:Person ;
         schema:name ?name ;
         dcterms:subject ?cat .
    FILTER(LCASE(STR(?cat)) = LCASE("{category}"))
  }}
}} ORDER BY ?name
""",

    # Which persons are known for a specific production?
    # Uses schema:subjectOf (URI link) and beng:knownForTitle (literal fallback)
    # param: production_title (partial match)
    "persons_known_for": PREFIXES + """
SELECT DISTINCT ?uri ?name ?knownFor WHERE {{
  {graph} {{
    ?uri a schema:Person ;
         schema:name ?name .
    {{
      ?uri schema:subjectOf ?productionUri .
      ?productionUri schema:name ?knownFor .
      FILTER(CONTAINS(LCASE(STR(?knownFor)), LCASE("{production_title}")))
    }} UNION {{
      ?uri beng:knownForTitle ?knownFor .
      FILTER(CONTAINS(LCASE(STR(?knownFor)), LCASE("{production_title}")))
    }}
  }}
}} ORDER BY ?name
""",

    # Most recently edited articles (freshness check)
    # param: limit (int, e.g. 20)
    "recently_edited": PREFIXES + """
SELECT DISTINCT ?uri ?name ?type ?modified WHERE {{
  {graph} {{
    ?uri schema:name ?name ;
         a ?type ;
         dcterms:modified ?modified .
    FILTER(?type IN (schema:Person, schema:CreativeWork, schema:Organization))
  }}
}} ORDER BY DESC(?modified) LIMIT {limit}
""",

    # Articles linked to a given GTAA concept scheme
    # param: scheme_uri (e.g. "http://data.beeldengeluid.nl/gtaa/Persoonsnamen")
    "articles_by_gtaa_scheme": PREFIXES + """
SELECT DISTINCT ?uri ?name ?gtaa WHERE {{
  {graph} {{
    ?uri schema:name ?name ;
         skos:exactMatch ?gtaa .
    FILTER(STRSTARTS(STR(?gtaa), "{scheme_uri}"))
  }}
}} ORDER BY ?name
""",

}


# ── Query runner ───────────────────────────────────────────────────────────────

def run_query(query: str, auth: tuple = AUTH) -> list[dict]:
    """Execute a SPARQL SELECT query and return results as a list of dicts."""
    r = requests.get(
        SPARQL_URL,
        params={"query": query},
        headers={"Accept": "application/sparql-results+json"},
        auth=auth,
        timeout=30,
    )
    r.raise_for_status()
    return [
        {k: v["value"] for k, v in row.items()}
        for row in r.json()["results"]["bindings"]
    ]


def get_query(name: str, **kwargs) -> str:
    """Fill a named query template with parameters."""
    template = QUERIES[name]
    return template.format(graph=GRAPH, **kwargs)
