#!/usr/bin/env python3.12
"""
SPARQL result correctness tests — requires Fuseki running on localhost:3030.

Two sections:
  1. Result correctness — count thresholds and known-entity presence checks.
     These are PASS/FAIL: they protect against query regressions and graph
     loading failures.
  2. Data quality checks — entity classification and coverage audits.
     These emit WARN (not FAIL) so they surface incrementally without
     blocking the test run. Add specific known-bad entities here as you
     find them.

Run:
  python3.12 evaluation/test_sparql.py
  python3.12 evaluation/test_sparql.py --verbose   # show all checks, not just problems
  python3.12 evaluation/test_sparql.py --quality   # data quality checks only

Exit code 0 = all correctness checks pass (warnings don't affect exit code).
"""

from __future__ import annotations

import sys
import argparse
import requests
from pathlib import Path
from dataclasses import dataclass, field

sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))
from sparql_queries import run_query, get_query, SPARQL_URL, AUTH, PREFIXES, GRAPH


# ── Connectivity check ─────────────────────────────────────────────────────────

def _check_fuseki() -> bool:
    try:
        r = requests.get(
            SPARQL_URL,
            params={"query": "SELECT (COUNT(*) AS ?n) WHERE { GRAPH <https://wiki.beeldengeluid.nl/graph> { ?s ?p ?o } }"},
            headers={"Accept": "application/sparql-results+json"},
            auth=AUTH,
            timeout=5,
        )
        r.raise_for_status()
        n = int(r.json()["results"]["bindings"][0]["n"]["value"])
        return n > 0
    except Exception:
        return False


# ── Test schemas ───────────────────────────────────────────────────────────────

@dataclass
class CountCheck:
    id: str
    query_name: str
    params: dict
    min_count: int
    max_count: int | None = None
    note: str = ""


@dataclass
class EntityCheck:
    id: str
    query_name: str
    params: dict
    must_contain: list[str] = field(default_factory=list)  # names that must appear
    must_not_contain: list[str] = field(default_factory=list)  # names that must NOT appear
    note: str = ""


@dataclass
class QualityCheck:
    id: str
    description: str
    query: str                    # raw SPARQL query
    expect_zero_rows: bool = True # True = warn if any rows returned
    note: str = ""


# ── Correctness: count thresholds ──────────────────────────────────────────────

COUNT_CHECKS: list[CountCheck] = [
    CountCheck(
        "c01", "all_broadcasters", {},
        min_count=10,
        note="Graph has 124 broadcasters; 10 is a conservative floor",
    ),
    CountCheck(
        "c02", "persons_by_function", {"function": "Presentator"},
        min_count=100,
        note="Graph has ~699 presentators",
    ),
    CountCheck(
        "c03", "persons_by_function", {"function": "Acteur"},
        min_count=100,
        note="Graph has ~786 acteurs",
    ),
    CountCheck(
        "c04", "persons_by_function", {"function": "Regisseur"},
        min_count=10,
        note="Directors are less numerous but should have at least 10",
    ),
    CountCheck(
        "c05", "persons_active_in_period", {"start_year": 1980, "end_year": 1990},
        min_count=1200,
        note="1,427 after Wikidata enrichment; threshold set to catch enrichment regressions",
    ),
    CountCheck(
        "c06", "persons_active_in_period", {"start_year": 1960, "end_year": 1975},
        min_count=800,
        note="935 after Wikidata enrichment",
    ),
    CountCheck(
        "c07", "productions_in_period", {"start_year": 1980, "end_year": 1989},
        min_count=200,
        note="Graph has ~1834 productions in 1980-1989",
    ),
    CountCheck(
        "c08", "productions_by_medium", {"medium": "Televisie"},
        min_count=1000,
        note="Graph has ~8195 TV productions — dominant medium",
    ),
    CountCheck(
        "c09", "productions_by_medium", {"medium": "Radio"},
        min_count=50,
        note="Radio productions should be well represented",
    ),
    CountCheck(
        "c10", "productions_by_genre", {"genre": "documentaire"},
        min_count=100,
        note="Graph has ~1462 documentaires",
    ),
    CountCheck(
        "c11", "recently_edited", {"limit": 10},
        min_count=10, max_count=10,
        note="LIMIT 10 must return exactly 10 (fails if < 10 articles have dcterms:modified)",
    ),
    CountCheck(
        "c12", "persons_by_category", {"category": "Acteur"},
        min_count=100,
        note="Graph has ~809 persons in category Acteur (via dcterms:subject)",
    ),
    CountCheck(
        "c13", "productions_by_broadcaster", {"broadcaster_name": "VPRO"},
        min_count=100,
        note="VPRO should have many linked productions (~935)",
    ),
    CountCheck(
        "c14", "productions_by_broadcaster", {"broadcaster_name": "NOS"},
        min_count=50,
        note="NOS should have many linked productions (~442)",
    ),
]


# ── Correctness: known entity presence ────────────────────────────────────────

ENTITY_CHECKS: list[EntityCheck] = [
    EntityCheck(
        "e01", "all_broadcasters", {},
        must_contain=["AVRO", "VARA", "VPRO", "KRO", "NCRV"],
        note="Major Dutch public broadcasters must be present",
    ),
    EntityCheck(
        "e02", "persons_by_function", {"function": "Presentator"},
        must_contain=["Mies Bouwman", "Ivo Niehe", "Willem Duys"],
        note="Well-known Dutch TV presenters",
    ),
    EntityCheck(
        "e03", "persons_active_in_period", {"start_year": 1980, "end_year": 1990},
        must_contain=["Mies Bouwman", "Ivo Niehe", "Joris Ivens"],
        must_not_contain=["Postbus 900"],
        note="Known persons active 1980-1990; Postbus 900 is a TV show, not a person",
    ),
    EntityCheck(
        "e04", "persons_active_in_period", {"start_year": 1950, "end_year": 1960},
        must_contain=["Mies Bouwman"],
        note="Mies Bouwman was active from the 1950s",
    ),
    EntityCheck(
        "e05", "productions_by_broadcaster", {"broadcaster_name": "VPRO"},
        must_contain=["Andere tijden"],
        note="'Andere tijden' is a well-known VPRO documentary series",
    ),
    EntityCheck(
        "e06", "all_broadcasters", {},
        must_contain=["AVRO", "VARA", "VPRO", "KRO", "NCRV"],
        must_not_contain=["Mies Bouwman", "Rob de Nijs"],
        note="Persons must not appear in the broadcaster list",
    ),
]


# ── Data quality checks ────────────────────────────────────────────────────────
# Each query should return zero rows if data is clean.
# Non-zero results are WARNs, not failures.

_DQ_PERSONS_AS_CREATIVE_WORK = PREFIXES + """
SELECT ?uri ?name WHERE {
  GRAPH <https://wiki.beeldengeluid.nl/graph> {
    ?uri a schema:Person ;
         schema:name ?name .
    # Heuristic: person articles whose names end in a year range in parentheses
    # are likely misclassified production articles.
    FILTER(REGEX(STR(?name), "\\\\(\\\\d{4}\\\\s*[-–]\\\\s*(\\\\d{4}|heden)\\\\)$"))
  }
}
"""

_DQ_CREATIVE_WORK_AS_PERSON = PREFIXES + """
SELECT ?uri ?name WHERE {
  GRAPH <https://wiki.beeldengeluid.nl/graph> {
    ?uri a schema:CreativeWork ;
         schema:name ?name ;
         schema:jobTitle ?title .
  }
} LIMIT 20
"""

_DQ_POSTBUS_900_TYPE = PREFIXES + """
SELECT ?uri ?type WHERE {
  GRAPH <https://wiki.beeldengeluid.nl/graph> {
    ?uri schema:name "Postbus 900" ;
         a ?type .
    FILTER(?type = schema:Person)
  }
}
"""

_DQ_PERSONS_NO_PERIOD = PREFIXES + """
SELECT (COUNT(?uri) AS ?n) WHERE {
  GRAPH <https://wiki.beeldengeluid.nl/graph> {
    ?uri a schema:Person .
    FILTER NOT EXISTS { ?uri beng:periodStart ?start }
  }
}
"""

_DQ_PRODUCTIONS_NO_MEDIUM = PREFIXES + """
SELECT (COUNT(?uri) AS ?n) WHERE {
  GRAPH <https://wiki.beeldengeluid.nl/graph> {
    ?uri a schema:CreativeWork .
    FILTER NOT EXISTS { ?uri beng:medium ?medium }
  }
}
"""

QUALITY_CHECKS: list[QualityCheck] = [
    QualityCheck(
        "q01",
        "schema:Person with name matching 'YYYY–YYYY' pattern (likely misclassified productions)",
        _DQ_PERSONS_AS_CREATIVE_WORK,
        expect_zero_rows=True,
        note="Production articles with year ranges in the title should be CreativeWork, not Person",
    ),
    QualityCheck(
        "q02",
        "schema:CreativeWork with schema:jobTitle (likely misclassified persons)",
        _DQ_CREATIVE_WORK_AS_PERSON,
        expect_zero_rows=True,
        note="jobTitle is a person property; CreativeWork with jobTitle suggests misclassification",
    ),
    QualityCheck(
        "q03",
        "Postbus 900 typed as schema:Person (regression guard — was misclassified, now fixed)",
        _DQ_POSTBUS_900_TYPE,
        expect_zero_rows=True,
        note="Postbus 900 is a TV programme; must remain schema:CreativeWork",
    ),
    QualityCheck(
        "q04",
        "Known omroepen (VPRO, VARA, NOS) still appearing as schema:contributor on productions",
        PREFIXES + """
SELECT ?prod ?prodName ?org ?orgName WHERE {
  GRAPH <https://wiki.beeldengeluid.nl/graph> {
    ?org a schema:Organization ;
         schema:name ?orgName .
    FILTER(?orgName IN ("VPRO", "VARA", "NOS", "AVRO", "KRO", "NCRV", "TROS", "EO"))
    ?prod schema:contributor ?org ;
          schema:name ?prodName .
  }
} LIMIT 10
""",
        expect_zero_rows=True,
        note="Omroepen should be linked via beng:broadcaster, not schema:contributor",
    ),
]

# Coverage stats — these don't pass/fail, just print numbers
_COVERAGE_QUERIES = [
    ("Total schema:Person",       PREFIXES + "SELECT (COUNT(?u) AS ?n) WHERE { GRAPH <https://wiki.beeldengeluid.nl/graph> { ?u a schema:Person } }"),
    ("Total schema:CreativeWork", PREFIXES + "SELECT (COUNT(?u) AS ?n) WHERE { GRAPH <https://wiki.beeldengeluid.nl/graph> { ?u a schema:CreativeWork } }"),
    ("Total schema:Organization", PREFIXES + "SELECT (COUNT(?u) AS ?n) WHERE { GRAPH <https://wiki.beeldengeluid.nl/graph> { ?u a schema:Organization } }"),
    ("Persons with periodStart",  PREFIXES + "SELECT (COUNT(?u) AS ?n) WHERE { GRAPH <https://wiki.beeldengeluid.nl/graph> { ?u a schema:Person ; beng:periodStart ?s } }"),
    ("Persons with jobTitle",     PREFIXES + "SELECT (COUNT(?u) AS ?n) WHERE { GRAPH <https://wiki.beeldengeluid.nl/graph> { ?u a schema:Person ; schema:jobTitle ?t } }"),
    ("Productions with medium",   PREFIXES + "SELECT (COUNT(?u) AS ?n) WHERE { GRAPH <https://wiki.beeldengeluid.nl/graph> { ?u a schema:CreativeWork ; beng:medium ?m } }"),
    ("Productions with genre",      PREFIXES + "SELECT (COUNT(?u) AS ?n) WHERE { GRAPH <https://wiki.beeldengeluid.nl/graph> { ?u a schema:CreativeWork ; schema:genre ?g } }"),
    ("Productions with broadcaster",PREFIXES + "SELECT (COUNT(?u) AS ?n) WHERE { GRAPH <https://wiki.beeldengeluid.nl/graph> { ?u a schema:CreativeWork ; beng:broadcaster ?b } }"),
    ("Articles with GTAA link",     PREFIXES + "SELECT (COUNT(?u) AS ?n) WHERE { GRAPH <https://wiki.beeldengeluid.nl/graph> { ?u skos:exactMatch ?gtaa } }"),
]


# ── Runner ─────────────────────────────────────────────────────────────────────

def _run_correctness(verbose: bool) -> tuple[int, int]:
    """Returns (failures, total)."""
    failures = 0
    total = 0

    print("── Count thresholds ──────────────────────────────────────────")
    for c in COUNT_CHECKS:
        total += 1
        try:
            rows = run_query(get_query(c.query_name, **c.params))
            n = len(rows)
            ok = n >= c.min_count and (c.max_count is None or n <= c.max_count)
        except Exception as exc:
            ok = False
            n = f"ERROR: {exc}"

        tag = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
            bounds = f">= {c.min_count}" + (f", <= {c.max_count}" if c.max_count else "")
            print(f"  [{tag}] {c.id}: {c.query_name}({c.params})")
            print(f"         expected {bounds}, got {n}")
            if c.note:
                print(f"         note: {c.note}")
        elif verbose:
            print(f"  [{tag}] {c.id}: {c.query_name}({c.params}) → {n} rows")

    print()
    print("── Entity presence ───────────────────────────────────────────")
    for e in ENTITY_CHECKS:
        total += 1
        try:
            rows = run_query(get_query(e.query_name, **e.params))
            names = {r.get("name", "") for r in rows}
            missing = [n for n in e.must_contain if n not in names]
            present_bad = [n for n in e.must_not_contain if n in names]
            ok = not missing and not present_bad
        except Exception as exc:
            ok = False
            missing, present_bad = [], []
            exc_msg = str(exc)

        tag = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
            print(f"  [{tag}] {e.id}: {e.query_name}({e.params})")
            if missing:
                print(f"         missing expected: {missing}")
            if present_bad:
                print(f"         found unexpected: {present_bad}")
            if e.note:
                print(f"         note: {e.note}")
        elif verbose:
            print(f"  [{tag}] {e.id}: {e.query_name}({e.params})")
            if e.must_contain:
                print(f"         confirmed present: {e.must_contain}")
            if e.must_not_contain:
                print(f"         confirmed absent:  {e.must_not_contain}")

    return failures, total


def _run_quality(verbose: bool) -> int:
    """Returns number of warnings triggered."""
    warnings = 0

    print("── Data quality checks ───────────────────────────────────────")
    for q in QUALITY_CHECKS:
        try:
            rows = run_query(q.query)
        except Exception as exc:
            print(f"  [ERROR] {q.id}: {q.description}")
            print(f"          {exc}")
            continue

        if q.expect_zero_rows and rows:
            warnings += 1
            print(f"  [WARN]  {q.id}: {q.description}")
            print(f"          {len(rows)} row(s) returned — expected zero")
            for r in rows[:5]:
                print(f"          → {r}")
            if len(rows) > 5:
                print(f"          … and {len(rows) - 5} more")
            if q.note:
                print(f"          note: {q.note}")
        elif verbose:
            tag = "OK" if q.expect_zero_rows else "INFO"
            print(f"  [{tag}]   {q.id}: {q.description}")
            if rows:
                print(f"          {len(rows)} row(s)")

    print()
    print("── Coverage stats ────────────────────────────────────────────")
    for label, query in _COVERAGE_QUERIES:
        try:
            rows = run_query(query)
            n = rows[0].get("n", "?") if rows else "?"
        except Exception as exc:
            n = f"ERROR: {exc}"
        print(f"  {label:35s} {n:>8}")

    return warnings


def run(verbose: bool = False, quality_only: bool = False) -> int:
    if not _check_fuseki():
        print("ERROR: Fuseki is not reachable at", SPARQL_URL)
        print("       Start Fuseki and ensure the wiki graph is loaded, then retry.")
        return 1

    failures = 0
    total = 0
    warnings = 0

    if not quality_only:
        failures, total = _run_correctness(verbose)
        print()

    warnings = _run_quality(verbose)

    print()
    if not quality_only:
        passed = total - failures
        print(f"Correctness: {passed}/{total} passed", end="")
        if failures:
            print(f"  ({failures} failed)")
        else:
            print("  ✓")
    if warnings:
        print(f"Data quality: {warnings} warning(s)")
    else:
        print("Data quality: clean ✓")
    print()

    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true",
                        help="Show all checks, not just failures/warnings")
    parser.add_argument("--quality", action="store_true",
                        help="Run data quality checks only (skip correctness tests)")
    args = parser.parse_args()
    sys.exit(run(verbose=args.verbose, quality_only=args.quality))
