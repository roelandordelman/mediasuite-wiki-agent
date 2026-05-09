#!/usr/bin/env python3
"""
Link wiki articles to GTAA concept URIs via SPARQL label matching.

Updates data/structured/{pageid}.json in-place, adding:
  gtaa_uri              (str | null)
  gtaa_match_confidence ("exact" | "alias" | "none")

Scheme mapping (verified against cat.apis.beeldengeluid.nl/sparql):
  persoon   → gtaa:Persoonsnamen   (385k entries; labels are inverted: "Nijs, Rob de")
  productie → gtaa:Programmatitels
  genre     → gtaa:Genre
  onderwerp → gtaa:Onderwerpen + gtaa:OnderwerpenBenG
  omroep    → gtaa:Namen

Re-running is safe: files already containing gtaa_match_confidence are skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

log = logging.getLogger(__name__)

SPARQL_ENDPOINT = "https://cat.apis.beeldengeluid.nl/sparql"

SCHEME_MAP: dict[str, list[str]] = {
    "persoon": ["http://data.beeldengeluid.nl/gtaa/Persoonsnamen"],
    "productie": ["http://data.beeldengeluid.nl/gtaa/Programmatitels"],
    "genre": ["http://data.beeldengeluid.nl/gtaa/Genre"],
    "onderwerp": [
        "http://data.beeldengeluid.nl/gtaa/Onderwerpen",
        "http://data.beeldengeluid.nl/gtaa/OnderwerpenBenG",
    ],
    "omroep": ["http://data.beeldengeluid.nl/gtaa/Namen"],
}

# Dutch/French prepositions that appear between first and last name
_TUSSENVOEGSELS = frozenset([
    "van", "de", "den", "der", "in", "op", "aan", "ten", "ter", "'t",
    "von", "vom", "zum", "la", "le", "du", "des",
])


# ── Name inversion ─────────────────────────────────────────────────────────────

def invert_dutch_name(name: str) -> str:
    """
    Convert natural-order Dutch name to GTAA inverted form.

    "Rob de Nijs"          → "Nijs, Rob de"
    "Paul van Vliet"       → "Vliet, Paul van"
    "Jan van den Berg"     → "Berg, Jan van den"
    "Léon Povel"           → "Povel, Léon"
    "Rob de Nijs (zanger)" → "Nijs, Rob de"   (disambiguation stripped)
    """
    name = re.sub(r'\s*\(.*?\)', '', name).strip()
    words = name.split()
    if len(words) <= 1:
        return name

    # Last word starting with a capital letter is the surname
    surname_idx = -1
    for i in range(len(words) - 1, -1, -1):
        w = words[i]
        if w[0].isupper():
            surname_idx = i
            break

    if surname_idx <= 0:
        return name

    # Consecutive lowercase words immediately before the surname = tussenvoegsel
    tussenvoegsel_start = surname_idx
    for i in range(surname_idx - 1, -1, -1):
        if words[i].lower() in _TUSSENVOEGSELS:
            tussenvoegsel_start = i
        else:
            break

    first_parts = words[:tussenvoegsel_start]
    tussenvoegsel_parts = words[tussenvoegsel_start:surname_idx]

    if not first_parts:
        return name

    inverted = words[surname_idx] + ", " + " ".join(first_parts)
    if tussenvoegsel_parts:
        inverted += " " + " ".join(tussenvoegsel_parts)
    return inverted


# ── SPARQL lookup ──────────────────────────────────────────────────────────────

def _sparql_get(session: requests.Session, label: str, scheme: str, predicate: str) -> str | None:
    """Return concept URI if label matches, else None."""
    escaped = label.replace("\\", "\\\\").replace('"', '\\"')
    query = (
        "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
        f"SELECT ?concept WHERE {{\n"
        f"  ?concept {predicate} ?l ;\n"
        f"           skos:inScheme <{scheme}> .\n"
        f'  FILTER(STR(?l) = "{escaped}")\n'
        f"}} LIMIT 1"
    )
    for attempt in range(3):
        try:
            r = session.get(
                SPARQL_ENDPOINT,
                params={"query": query, "format": "json"},
                timeout=20,
            )
            r.raise_for_status()
            bindings = r.json()["results"]["bindings"]
            return bindings[0]["concept"]["value"] if bindings else None
        except requests.Timeout:
            log.debug("Timeout for %r (attempt %d)", label, attempt + 1)
            time.sleep(1 * (attempt + 1))
        except Exception as exc:
            log.debug("SPARQL error for %r: %s", label, exc)
            return None
    return None


def lookup_gtaa(
    session: requests.Session,
    labels: list[str],
    schemes: list[str],
) -> tuple[str | None, str]:
    """
    Try prefLabel then altLabel across all candidate labels and schemes.
    Returns (gtaa_uri, confidence) where confidence ∈ {"exact", "alias", "none"}.
    """
    for predicate, confidence in [("skos:prefLabel", "exact"), ("skos:altLabel", "alias")]:
        for label in labels:
            for scheme in schemes:
                uri = _sparql_get(session, label, scheme, predicate)
                if uri:
                    return uri, confidence
    return None, "none"


# ── Label preparation ──────────────────────────────────────────────────────────

def _build_labels(record: dict) -> list[str]:
    """Return candidate labels to try, in preference order."""
    title = record["title"]
    article_type = record.get("article_type", "other")

    if article_type == "persoon":
        # GTAA Persoonsnamen uses inverted form; try both article title and
        # the full name from the infobox if it differs.
        labels: list[str] = []
        inverted_title = invert_dutch_name(title)
        labels.append(inverted_title)

        naam = (record.get("persoon") or {}).get("naam", "")
        if naam and naam != title:
            inverted_naam = invert_dutch_name(naam)
            if inverted_naam not in labels:
                labels.append(inverted_naam)
        return labels
    else:
        # For productions, genres, onderwerpen: use the title directly,
        # also try without parenthetical disambiguation.
        clean = re.sub(r'\s*\(.*?\)', '', title).strip()
        labels = [clean]
        if clean != title:
            labels.append(title)
        return labels


# ── Per-file processor ─────────────────────────────────────────────────────────

def _process_file(path: Path, session: requests.Session) -> tuple[str, str | None, str]:
    """Return (title, gtaa_uri, confidence). Writes result to file."""
    try:
        record = json.loads(path.read_text())
    except Exception as exc:
        return (str(path), None, f"read_error: {exc}")

    if "gtaa_match_confidence" in record:
        return record.get("title", str(path)), record.get("gtaa_uri"), "skipped"

    article_type = record.get("article_type", "other")
    schemes = SCHEME_MAP.get(article_type)

    if not schemes:
        record["gtaa_uri"] = None
        record["gtaa_match_confidence"] = "none"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        return record.get("title", ""), None, "none"

    labels = _build_labels(record)
    gtaa_uri, confidence = lookup_gtaa(session, labels, schemes)

    record["gtaa_uri"] = gtaa_uri
    record["gtaa_match_confidence"] = confidence
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return record.get("title", ""), gtaa_uri, confidence


# ── Main ───────────────────────────────────────────────────────────────────────

def run(structured_dir: Path, workers: int, types: list[str]) -> None:
    files = sorted(structured_dir.glob("*.json"))

    # Pre-filter to linkable types to avoid loading every file twice
    target_types = set(types) if types else set(SCHEME_MAP)

    linkable: list[Path] = []
    skippable = 0
    for path in files:
        try:
            rec = json.loads(path.read_text())
            if "gtaa_match_confidence" in rec:
                skippable += 1
                continue
            if rec.get("article_type") in target_types:
                linkable.append(path)
        except Exception:
            pass

    log.info(
        "%d files total | %d already linked | %d to process (types: %s)",
        len(files), skippable, len(linkable), sorted(target_types),
    )

    counts: dict[str, int] = {"exact": 0, "alias": 0, "none": 0, "error": 0}

    def make_session() -> requests.Session:
        s = requests.Session()
        s.headers["Accept"] = "application/sparql-results+json"
        s.headers["User-Agent"] = (
            "mediasuite-wiki-agent/1.0 (GTAA linker; "
            "https://github.com/beeldengeluid/mediasuite-wiki-agent)"
        )
        return s

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Each worker gets its own session (requests.Session is not thread-safe)
        sessions = {i: make_session() for i in range(workers)}
        future_to_path = {}
        worker_idx = 0
        for path in linkable:
            sess = sessions[worker_idx % workers]
            future_to_path[pool.submit(_process_file, path, sess)] = path
            worker_idx += 1

        for future in as_completed(future_to_path):
            title, gtaa_uri, confidence = future.result()
            if confidence == "skipped":
                pass
            elif confidence.startswith("read_error"):
                counts["error"] += 1
            else:
                counts[confidence] = counts.get(confidence, 0) + 1
                if confidence == "exact":
                    log.debug("MATCH  %s → %s", title, gtaa_uri)

            done += 1
            if done % 200 == 0 or done == len(linkable):
                total = counts["exact"] + counts["alias"] + counts["none"]
                match_rate = (counts["exact"] + counts["alias"]) / total if total else 0
                log.info(
                    "Progress: %d / %d | exact %d | alias %d | none %d | match rate %.1f%%",
                    done, len(linkable),
                    counts["exact"], counts["alias"], counts["none"],
                    match_rate * 100,
                )

    log.info("Done. Results: %s", counts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Link wiki articles to GTAA concept URIs via SPARQL"
    )
    parser.add_argument("--structured-dir", default="data/structured", type=Path)
    parser.add_argument(
        "--workers", default=5, type=int,
        help="Parallel SPARQL workers (default: 5)",
    )
    parser.add_argument(
        "--types", nargs="+",
        default=list(SCHEME_MAP),
        metavar="TYPE",
        help=f"Article types to link (default: all — {list(SCHEME_MAP)})",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    run(args.structured_dir, args.workers, args.types)


if __name__ == "__main__":
    main()
