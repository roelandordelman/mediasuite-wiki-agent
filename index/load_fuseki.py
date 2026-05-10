#!/usr/bin/env python3
"""
Load data/wiki.ttl into a local Fuseki instance.

Follows the same pattern as mediasuite-knowledge-base/pipelines/graph/build_graph.py:
  1. Validate the Turtle file with rdflib
  2. Poll Fuseki /$/ping until ready
  3. Create the dataset if it does not exist
  4. Upload via SPARQL Graph Store Protocol (PUT to named graph)
  5. Verify with a COUNT query

Local Fuseki (Docker):
  docker run -d --name fuseki -p 3030:3030 \\
    -v $(pwd)/stores/fuseki_data:/fuseki/databases \\
    -e ADMIN_PASSWORD=admin \\
    stain/jena-fuseki

Swap to production: set FUSEKI_URL env var.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

import requests
from rdflib import ConjunctiveGraph, Graph

log = logging.getLogger(__name__)

FUSEKI_URL   = os.getenv("FUSEKI_URL", "http://localhost:3030")
DATASET      = os.getenv("FUSEKI_DATASET", "wiki")
GRAPH_URI    = "https://wiki.beeldengeluid.nl/graph"
ADMIN_USER   = os.getenv("FUSEKI_USER", "admin")
ADMIN_PASS   = os.getenv("FUSEKI_PASSWORD", "admin")
AUTH         = (ADMIN_USER, ADMIN_PASS)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _wait_for_fuseki(timeout: int = 30) -> bool:
    ping = f"{FUSEKI_URL}/$/ping"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(ping, timeout=3)
            if r.ok:
                log.info("Fuseki is ready at %s", FUSEKI_URL)
                return True
        except requests.ConnectionError:
            pass
        time.sleep(2)
    return False


def _ensure_dataset() -> None:
    datasets_url = f"{FUSEKI_URL}/$/datasets"
    r = requests.get(f"{datasets_url}/{DATASET}", auth=AUTH, timeout=10)
    if r.status_code == 404:
        log.info("Creating dataset %r …", DATASET)
        r = requests.post(
            datasets_url,
            data={"dbName": DATASET, "dbType": "tdb2"},
            auth=AUTH,
            timeout=10,
        )
        r.raise_for_status()
        log.info("Dataset %r created.", DATASET)
    else:
        r.raise_for_status()
        log.info("Dataset %r already exists.", DATASET)


def _upload_graph(ttl_path: Path) -> None:
    graph_store_url = f"{FUSEKI_URL}/{DATASET}/data"
    ttl_bytes = ttl_path.read_bytes()
    log.info("Uploading %s (%.1f MB) …", ttl_path, len(ttl_bytes) / 1_000_000)
    r = requests.put(
        graph_store_url,
        params={"graph": GRAPH_URI},
        data=ttl_bytes,
        headers={"Content-Type": "text/turtle; charset=utf-8"},
        auth=AUTH,
        timeout=120,
    )
    r.raise_for_status()
    log.info("Upload complete.")


def _verify_count() -> int:
    sparql_url = f"{FUSEKI_URL}/{DATASET}/sparql"
    query = f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{GRAPH_URI}> {{ ?s ?p ?o }} }}"
    r = requests.get(
        sparql_url,
        params={"query": query},
        headers={"Accept": "application/sparql-results+json"},
        auth=AUTH,
        timeout=30,
    )
    r.raise_for_status()
    n = int(r.json()["results"]["bindings"][0]["n"]["value"])
    log.info("Graph contains %d triples.", n)
    return n


# ── Main ───────────────────────────────────────────────────────────────────────

def load(ttl_path: Path, dry_run: bool = False) -> None:
    log.info("Validating %s with rdflib …", ttl_path)
    g = Graph()
    g.parse(str(ttl_path), format="turtle")
    log.info("Valid Turtle: %d triples.", len(g))

    if dry_run:
        log.info("Dry run — stopping before upload.")
        return

    if not _wait_for_fuseki():
        log.error(
            "Fuseki not reachable at %s after 30 s.\n\n"
            "Start it with:\n"
            "  docker run -d --name fuseki -p 3030:3030 \\\n"
            "    -v $(pwd)/stores/fuseki_data:/fuseki/databases \\\n"
            "    -e ADMIN_PASSWORD=admin \\\n"
            "    stain/jena-fuseki",
            FUSEKI_URL,
        )
        raise SystemExit(1)

    _ensure_dataset()
    _upload_graph(ttl_path)
    n = _verify_count()
    log.info(
        "Done. SPARQL endpoint: %s/%s/sparql  (%d triples in <%s>)",
        FUSEKI_URL, DATASET, n, GRAPH_URI,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Load wiki.ttl into Fuseki")
    parser.add_argument("--ttl", default="data/wiki.ttl", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Validate only, do not upload")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    load(args.ttl, args.dry_run)


if __name__ == "__main__":
    main()
