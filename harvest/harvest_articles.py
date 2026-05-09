#!/usr/bin/env python3
"""
Harvest all articles from wiki.beeldengeluid.nl via the MediaWiki Action API.

Two-phase process:
  1. Enumerate all page IDs (saved to data/pagelist.json as a checkpoint).
  2. Fetch content + categories + timestamp in batches, saving one JSON file
     per article to data/articles/{pageid}.json.

Re-running is safe: already-downloaded articles are skipped.
"""

import argparse
import json
import logging
import time
from pathlib import Path

import requests

API_URL = "https://wiki.beeldengeluid.nl/api.php"
WIKI_BASE = "https://wiki.beeldengeluid.nl/index.php"

SESSION_HEADERS = {
    "User-Agent": (
        "mediasuite-wiki-agent/1.0 "
        "(harvest pipeline; https://github.com/beeldengeluid/mediasuite-wiki-agent)"
    )
}

log = logging.getLogger(__name__)


def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(SESSION_HEADERS)
    return s


def enumerate_pages(session: requests.Session) -> list[dict]:
    """Return [{pageid, title}, ...] for every article in namespace 0."""
    pages: list[dict] = []
    params: dict = {
        "action": "query",
        "list": "allpages",
        "aplimit": 500,
        "apnamespace": 0,
        "format": "json",
    }
    while True:
        resp = session.get(API_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        batch = data["query"]["allpages"]
        pages.extend({"pageid": p["pageid"], "title": p["title"]} for p in batch)
        log.info("Enumerated %d pages so far …", len(pages))
        if "continue" not in data:
            break
        params["apcontinue"] = data["continue"]["apcontinue"]
        time.sleep(0.3)
    return pages


def _strip_ns(category_title: str) -> str:
    """Remove 'Categorie:' / 'Category:' namespace prefix."""
    if ":" in category_title:
        return category_title.split(":", 1)[1]
    return category_title


def fetch_batch(session: requests.Session, pageids: list[int]) -> dict[int, dict]:
    """
    Fetch wikitext + categories + last-edit timestamp for up to 50 page IDs.
    Returns {pageid: article_dict}.
    """
    params: dict = {
        "action": "query",
        "pageids": "|".join(str(p) for p in pageids),
        "prop": "revisions|categories",
        "rvprop": "content|timestamp",
        "rvslots": "main",
        "cllimit": 500,
        "format": "json",
    }
    resp = session.get(API_URL, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    result: dict[int, dict] = {}
    for pageid_str, page in data["query"]["pages"].items():
        pageid = int(pageid_str)
        if pageid < 0:
            # API encodes missing / invalid pages with negative IDs
            log.debug("Skipping missing page id %d", pageid)
            continue

        revisions = page.get("revisions", [])
        if not revisions:
            log.debug("No revisions for pageid %d (%s)", pageid, page.get("title"))
            continue

        rev = revisions[0]
        # rvslots=main → new slot-based format
        slots = rev.get("slots", {})
        wikitext = slots.get("main", {}).get("*", "") or rev.get("*", "")
        timestamp = rev.get("timestamp", "")

        categories = [_strip_ns(c["title"]) for c in page.get("categories", [])]

        title = page["title"]
        url_title = title.replace(" ", "_")

        result[pageid] = {
            "pageid": pageid,
            "title": title,
            "url": f"{WIKI_BASE}/{url_title}",
            "categories": categories,
            "last_edited": timestamp,
            "wikitext": wikitext,
        }
    return result


def harvest(
    output_dir: Path,
    pagelist_path: Path,
    batch_size: int,
    delay: float,
) -> None:
    session = get_session()
    output_dir.mkdir(parents=True, exist_ok=True)
    pagelist_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Phase 1: enumerate (or load cached page list) ────────────────────────
    if pagelist_path.exists():
        log.info("Loading cached page list from %s", pagelist_path)
        all_pages: list[dict] = json.loads(pagelist_path.read_text())
    else:
        log.info("Enumerating all pages from MediaWiki API …")
        all_pages = enumerate_pages(session)
        pagelist_path.write_text(
            json.dumps(all_pages, ensure_ascii=False, indent=2)
        )
        log.info("Discovered %d pages → saved to %s", len(all_pages), pagelist_path)

    # ── Phase 2: fetch content, skipping already-saved articles ──────────────
    pending = [
        p["pageid"]
        for p in all_pages
        if not (output_dir / f"{p['pageid']}.json").exists()
    ]
    already_done = len(all_pages) - len(pending)
    log.info(
        "%d total pages | %d already downloaded | %d to fetch",
        len(all_pages),
        already_done,
        len(pending),
    )

    fetched = errors = 0
    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        try:
            articles = fetch_batch(session, batch)
            for pageid, article in articles.items():
                path = output_dir / f"{pageid}.json"
                path.write_text(
                    json.dumps(article, ensure_ascii=False, indent=2)
                )
            fetched += len(articles)
            # Warn if API returned fewer articles than requested
            if len(articles) < len(batch):
                missing = set(batch) - set(articles)
                log.warning("Batch %d: %d page(s) missing from response: %s",
                            i // batch_size, len(missing), missing)
        except requests.RequestException as exc:
            log.error("Batch %d failed (network): %s", i // batch_size, exc)
            errors += 1
        except Exception as exc:
            log.error("Batch %d failed (unexpected): %s", i // batch_size, exc)
            errors += 1

        done = i + len(batch)
        if done % (batch_size * 20) == 0 or done >= len(pending):
            log.info(
                "Progress: %d / %d fetched | %d saved total | %d errors",
                done, len(pending), already_done + fetched, errors,
            )

        time.sleep(delay)

    log.info(
        "Harvest complete. %d articles saved, %d errors.",
        already_done + fetched,
        errors,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Harvest all B&G Wiki articles via the MediaWiki Action API."
    )
    parser.add_argument(
        "--output-dir",
        default="data/articles",
        type=Path,
        metavar="DIR",
        help="Directory to write per-article JSON files (default: data/articles)",
    )
    parser.add_argument(
        "--pagelist",
        default="data/pagelist.json",
        type=Path,
        metavar="FILE",
        help="Checkpoint file for the enumerated page list (default: data/pagelist.json)",
    )
    parser.add_argument(
        "--batch-size",
        default=50,
        type=int,
        metavar="N",
        help="Pages per API request (max 50 for anonymous access, default: 50)",
    )
    parser.add_argument(
        "--delay",
        default=0.5,
        type=float,
        metavar="SECS",
        help="Seconds to wait between batch requests (default: 0.5)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    harvest(
        output_dir=args.output_dir,
        pagelist_path=args.pagelist,
        batch_size=args.batch_size,
        delay=args.delay,
    )


if __name__ == "__main__":
    main()
