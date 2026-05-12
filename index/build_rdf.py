#!/usr/bin/env python3
"""
Convert structured article records to RDF Turtle.

Reads:  data/structured/{pageid}.json
Writes: data/wiki.ttl

Vocabulary:
  schema:Person / schema:CreativeWork / schema:Organization
  schema.org properties for names, dates, genres, contributors
  skos:exactMatch for GTAA URIs
  dcterms:modified for last-edited timestamps
  beng: custom namespace for wiki-specific properties (periodStart/End, medium)

Named graph: https://wiki.beeldengeluid.nl/graph
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from urllib.parse import quote

from urllib.parse import quote as _pct_encode

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, RDF, SKOS, XSD

log = logging.getLogger(__name__)

# ── Namespaces ─────────────────────────────────────────────────────────────────

SCHEMA = Namespace("https://schema.org/")
BENG   = Namespace("https://wiki.beeldengeluid.nl/vocab#")

GRAPH_URI = URIRef("https://wiki.beeldengeluid.nl/graph")


# ── URI helpers ────────────────────────────────────────────────────────────────

def _uri(url: str) -> URIRef:
    """Create a URIRef, percent-encoding characters invalid in URIs (e.g. quotes in titles)."""
    return URIRef(_pct_encode(url, safe=':/?=#&%+@!$\'()*,;-._~[]'))


# ── Type-specific triple builders ──────────────────────────────────────────────

def _add_persoon(g: Graph, uri: URIRef, record: dict, title_to_uri: dict[str, str]) -> None:
    p = record["persoon"]
    g.add((uri, RDF.type, SCHEMA.Person))
    g.add((uri, SCHEMA.name, Literal(record["title"])))

    naam = p.get("naam") or ""
    if naam and naam != record["title"]:
        g.add((uri, SCHEMA.alternateName, Literal(naam)))

    if p.get("birth_date"):
        g.add((uri, SCHEMA.birthDate, Literal(p["birth_date"])))
    if p.get("birth_place"):
        g.add((uri, SCHEMA.birthPlace, Literal(p["birth_place"])))
    if p.get("death_date"):
        g.add((uri, SCHEMA.deathDate, Literal(p["death_date"])))
    if p.get("death_place"):
        g.add((uri, SCHEMA.deathPlace, Literal(p["death_place"])))

    for fn in p.get("functions") or []:
        g.add((uri, SCHEMA.jobTitle, Literal(fn)))

    if p.get("period_start"):
        g.add((uri, BENG.periodStart, Literal(p["period_start"], datatype=XSD.integer)))
    if p.get("period_end"):
        g.add((uri, BENG.periodEnd, Literal(p["period_end"], datatype=XSD.integer)))

    for name in p.get("collaborators") or []:
        target = title_to_uri.get(name)
        if target:
            g.add((uri, SCHEMA.colleague, _uri(target)))
        else:
            g.add((uri, BENG.collaboratorName, Literal(name)))

    for name in p.get("known_for") or []:
        target = title_to_uri.get(name)
        if target:
            g.add((uri, SCHEMA.subjectOf, _uri(target)))
        else:
            g.add((uri, BENG.knownForTitle, Literal(name)))


def _add_productie(
    g: Graph,
    uri: URIRef,
    record: dict,
    title_to_uri: dict[str, str],
    title_to_type: dict[str, str],
) -> None:
    p = record["productie"]
    g.add((uri, RDF.type, SCHEMA.CreativeWork))
    g.add((uri, SCHEMA.name, Literal(record["title"])))

    for genre in p.get("genre") or []:
        g.add((uri, SCHEMA.genre, Literal(genre)))

    if p.get("medium"):
        g.add((uri, BENG.medium, Literal(p["medium"])))

    if p.get("period_start"):
        g.add((uri, BENG.periodStart, Literal(p["period_start"], datatype=XSD.integer)))
    if p.get("period_end"):
        g.add((uri, BENG.periodEnd, Literal(p["period_end"], datatype=XSD.integer)))

    for name in p.get("persons") or []:
        target_url = title_to_uri.get(name)
        if target_url:
            target_uri = _uri(target_url)
            kind = title_to_type.get(name, "")
            if kind == "omroep":
                g.add((uri, BENG.broadcaster, target_uri))
            elif kind == "producent_bedrijf":
                g.add((uri, SCHEMA.productionCompany, target_uri))
            else:
                g.add((uri, SCHEMA.contributor, target_uri))
        else:
            g.add((uri, BENG.contributorName, Literal(name)))


def _add_omroep(g: Graph, uri: URIRef, record: dict) -> None:
    g.add((uri, RDF.type, SCHEMA.Organization))
    g.add((uri, SCHEMA.name, Literal(record["title"])))


# ── Main builder ───────────────────────────────────────────────────────────────

def build_rdf(structured_dir: Path, output_path: Path) -> int:
    files = sorted(structured_dir.glob("*.json"))
    log.info("Reading %d structured files …", len(files))

    # Build title → url and title → article_type lookups for cross-linking
    title_to_uri: dict[str, str] = {}
    title_to_type: dict[str, str] = {}
    records: list[dict] = []
    for path in files:
        try:
            r = json.loads(path.read_text())
            records.append(r)
            if r.get("url") and r.get("title"):
                title_to_uri[r["title"]] = r["url"]
                title_to_type[r["title"]] = r.get("article_type", "")
        except Exception as exc:
            log.warning("Skipping %s: %s", path.name, exc)

    log.info("Building RDF graph …")
    g = Graph()
    g.bind("schema",  SCHEMA)
    g.bind("skos",    SKOS)
    g.bind("dcterms", DCTERMS)
    g.bind("beng",    BENG)
    g.bind("xsd",     XSD)

    added = skipped = 0
    for record in records:
        article_type = record.get("article_type")
        if article_type in ("redirect", "other"):
            skipped += 1
            continue

        uri = _uri(record["url"])

        if article_type == "persoon" and record.get("persoon"):
            _add_persoon(g, uri, record, title_to_uri)
        elif article_type == "productie" and record.get("productie"):
            _add_productie(g, uri, record, title_to_uri, title_to_type)
        elif article_type in ("omroep", "producent_bedrijf"):
            _add_omroep(g, uri, record)
        else:
            skipped += 1
            continue

        # Common fields for all typed articles
        g.add((uri, SCHEMA.url, uri))
        if record.get("last_edited"):
            g.add((uri, DCTERMS.modified, Literal(record["last_edited"], datatype=XSD.dateTime)))
        for cat in record.get("categories") or []:
            g.add((uri, DCTERMS.subject, Literal(cat)))
        if record.get("gtaa_uri"):
            g.add((uri, SKOS.exactMatch, URIRef(record["gtaa_uri"])))

        added += 1

    log.info("Serialising %d triples from %d articles (%d skipped) …",
             len(g), added, skipped)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(destination=str(output_path), format="turtle")
    log.info("Wrote %s", output_path)
    return len(g)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert structured records → RDF Turtle")
    parser.add_argument("--structured-dir", default="data/structured", type=Path)
    parser.add_argument("--output", default="data/wiki.ttl", type=Path)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    n = build_rdf(args.structured_dir, args.output)
    log.info("Done. %d triples written.", n)


if __name__ == "__main__":
    main()
