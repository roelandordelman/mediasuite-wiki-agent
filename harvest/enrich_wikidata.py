#!/usr/bin/env python3.12
"""
Enrich person structured records with birth/death data from Wikidata.

For persons that have a GTAA URI but no period_start, queries Wikidata via
property P1741 (GTAA ID) to retrieve birth date (P569) and death date (P570).
Computes an estimated active period:
  period_start = birth_year + 20   (earliest plausible professional start)
  period_end   = death_year        (None if still alive or unknown)

Only fills gaps — never overwrites data that already came from the wiki infobox.
Records are updated in-place in data/structured/.

Run AFTER link_gtaa.py, BEFORE build_rdf.py:
  python3.12 harvest/enrich_wikidata.py
  python3.12 harvest/enrich_wikidata.py --dry-run   # preview without writing
  python3.12 harvest/enrich_wikidata.py --force      # re-enrich already-enriched files

After running, rebuild the graph:
  python3.12 index/build_rdf.py
  python3.12 index/load_fuseki.py
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "mediasuite-wiki-agent/1.0 (https://github.com/roelandordelman/mediasuite-wiki-agent)"

# Rough estimate: media professionals typically start working in their 20s
CAREER_START_OFFSET = 20

# Wikidata SPARQL times out on large VALUES blocks; 10 IDs per batch is safe
BATCH_SIZE = 10

# Polite delay between Wikidata requests (seconds)
REQUEST_DELAY = 2.0

# Max retries on 429 Too Many Requests
MAX_RETRIES = 3


def _gtaa_id(gtaa_uri: str) -> str | None:
    """Extract numeric GTAA ID from a full GTAA URI."""
    if not gtaa_uri:
        return None
    part = gtaa_uri.rstrip("/").split("/")[-1]
    return part if part.isdigit() else None


def _extract_year(iso_date: str) -> int | None:
    """Return the year from an ISO date string like '1945-03-15T00:00:00Z'."""
    try:
        return int(iso_date[:4])
    except (ValueError, TypeError):
        return None


def _query_wikidata(gtaa_ids: list[str]) -> dict[str, dict]:
    """
    Batch-query Wikidata for birth and death dates.
    Returns {gtaa_id: {"birth_year": int|None, "death_year": int|None, "wikidata_uri": str}}.
    """
    values = " ".join(f'"{g}"' for g in gtaa_ids)
    query = f"""
SELECT ?gtaaId ?item ?birthDate ?deathDate WHERE {{
  VALUES ?gtaaId {{ {values} }}
  ?item wdt:P1741 ?gtaaId .
  OPTIONAL {{ ?item wdt:P569 ?birthDate }}
  OPTIONAL {{ ?item wdt:P570 ?deathDate }}
}}
"""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(
                WIKIDATA_SPARQL,
                params={"query": query, "format": "json"},
                headers={"User-Agent": USER_AGENT},
                timeout=45,
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 10 * (attempt + 1)))
                log.warning("Rate limited by Wikidata; waiting %ds …", retry_after)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            break
        except requests.RequestException as exc:
            log.warning("Wikidata request failed (attempt %d/%d): %s",
                        attempt + 1, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES - 1:
                time.sleep(REQUEST_DELAY * (attempt + 1))
    else:
        return {}

    results: dict[str, dict] = {}
    for row in resp.json()["results"]["bindings"]:
        gtaa_id = row["gtaaId"]["value"]
        wikidata_uri = row["item"]["value"]
        birth_year = _extract_year(row.get("birthDate", {}).get("value"))
        death_year = _extract_year(row.get("deathDate", {}).get("value"))
        # Last write wins if multiple Wikidata items share a GTAA ID (rare)
        results[gtaa_id] = {
            "wikidata_uri": wikidata_uri,
            "birth_year": birth_year,
            "death_year": death_year,
        }
    return results


def _collect_targets(structured_dir: Path, force: bool) -> list[tuple[str, Path]]:
    """
    Return [(gtaa_id, file_path)] for persons with GTAA link but no period_start.
    Skips already-enriched files unless --force.
    """
    targets = []
    for path in sorted(structured_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except Exception:
            continue

        if record.get("article_type") != "persoon":
            continue

        # Skip if already enriched (unless --force)
        if not force and record.get("wikidata_enriched"):
            continue

        # Skip if period already in wiki infobox
        persoon = record.get("persoon") or {}
        if persoon.get("period_start") is not None:
            continue

        gtaa_uri = record.get("gtaa_uri", "")
        gtaa_id = _gtaa_id(gtaa_uri)
        if gtaa_id:
            targets.append((gtaa_id, path))

    return targets


def run(structured_dir: Path, dry_run: bool = False, force: bool = False) -> None:
    targets = _collect_targets(structured_dir, force)
    log.info("Found %d persons with GTAA link but no period_start", len(targets))
    if not targets:
        log.info("Nothing to enrich.")
        return

    enriched = skipped = errors = 0
    gtaa_id_to_path: dict[str, Path] = {gid: path for gid, path in targets}

    # Process in batches
    all_gtaa_ids = list(gtaa_id_to_path.keys())
    wikidata_results: dict[str, dict] = {}

    for i in range(0, len(all_gtaa_ids), BATCH_SIZE):
        batch = all_gtaa_ids[i : i + BATCH_SIZE]
        log.info("Querying Wikidata batch %d–%d of %d …",
                 i + 1, min(i + BATCH_SIZE, len(all_gtaa_ids)), len(all_gtaa_ids))
        batch_results = _query_wikidata(batch)
        wikidata_results.update(batch_results)
        log.info("  Batch returned %d hits", len(batch_results))
        if i + BATCH_SIZE < len(all_gtaa_ids):
            time.sleep(REQUEST_DELAY)

    log.info("Wikidata returned data for %d / %d persons", len(wikidata_results), len(targets))

    # Write enrichments
    for gtaa_id, path in gtaa_id_to_path.items():
        wd = wikidata_results.get(gtaa_id)
        if not wd:
            skipped += 1
            continue

        birth_year = wd["birth_year"]
        death_year = wd["death_year"]
        wikidata_uri = wd["wikidata_uri"]

        if birth_year is None:
            # Can't compute active period without birth year
            skipped += 1
            log.debug("No birth year for GTAA %s (%s)", gtaa_id, wikidata_uri)
            continue

        period_start = birth_year + CAREER_START_OFFSET
        period_end = death_year  # None = still active or unknown

        try:
            record = json.loads(path.read_text())
        except Exception as exc:
            log.error("Cannot read %s: %s", path, exc)
            errors += 1
            continue

        # Ensure persoon dict exists (persons with empty infobox have no key)
        if not record.get("persoon"):
            record["persoon"] = {}

        record["persoon"]["period_start"] = period_start
        record["persoon"]["period_end"] = period_end
        record["wikidata_uri"] = wikidata_uri
        record["wikidata_birth_year"] = birth_year
        record["wikidata_death_year"] = death_year
        record["wikidata_enriched"] = True

        title = record.get("title", path.stem)
        log.info("  %-35s born %d → active %d–%s  %s",
                 title, birth_year, period_start,
                 str(period_end) if period_end else "?",
                 wikidata_uri.split("/")[-1])

        if not dry_run:
            try:
                path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
                enriched += 1
            except Exception as exc:
                log.error("Cannot write %s: %s", path, exc)
                errors += 1
        else:
            enriched += 1

    prefix = "[DRY RUN] " if dry_run else ""
    log.info("%sDone. enriched=%d  no_wikidata_match=%d  errors=%d",
             prefix, enriched, skipped, errors)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich person records with Wikidata birth/death dates"
    )
    parser.add_argument("--structured-dir", default="data/structured", type=Path)
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without writing files")
    parser.add_argument("--force", action="store_true",
                        help="Re-enrich already-enriched files")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    run(args.structured_dir, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()
