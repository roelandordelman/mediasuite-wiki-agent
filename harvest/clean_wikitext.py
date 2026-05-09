#!/usr/bin/env python3
"""
Clean MediaWiki markup from harvested articles → plain text.

Reads:  data/articles/{pageid}.json
Writes: data/cleaned/{pageid}.json  (plain text + metadata)

Re-running is safe: already-cleaned files are skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

import mwparserfromhell

log = logging.getLogger(__name__)

_REMOVE_NAMESPACES = frozenset([
    "bestand", "afbeelding", "file", "image", "gallery", "categorie", "category",
])


def _is_redirect(wikitext: str) -> bool:
    first = wikitext.strip()[:20].lower()
    return first.startswith("#redirect") or first.startswith("#doorverwijzing")


def _infobox_name(wikitext: str) -> str | None:
    m = re.search(r'\{\{\s*(Infobox\s+\S+)', wikitext, re.IGNORECASE)
    return m.group(1).strip() if m else None


def clean_wikitext(wikitext: str) -> str:
    """Strip MediaWiki markup and return plain text."""
    if _is_redirect(wikitext):
        return ""

    try:
        wikicode = mwparserfromhell.parse(wikitext)
    except Exception:
        # Fallback: crude regex strip
        text = re.sub(r'\{\{.*?\}\}', '', wikitext, flags=re.DOTALL)
        text = re.sub(r'\[\[.*?\]\]', '', text)
        return re.sub(r'\s+', ' ', text).strip()

    # Remove infobox templates (their content goes to the structured store)
    for tmpl in wikicode.filter_templates(recursive=False):
        if tmpl.name.strip().lower().startswith("infobox"):
            try:
                wikicode.remove(tmpl)
            except ValueError:
                pass

    # Remove file/image/category wikilinks entirely
    for link in wikicode.filter_wikilinks(recursive=True):
        ns = link.title.strip().split(":", 1)[0].strip().lower()
        if ns in _REMOVE_NAMESPACES:
            try:
                wikicode.remove(link)
            except ValueError:
                pass

    # strip_code converts headings, bold/italic, wikilinks → plain text
    text = wikicode.strip_code(normalize=True, collapse=True)

    # Remove imagemap blocks and residual HTML tags
    text = re.sub(r'<imagemap>.*?</imagemap>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)

    # Collapse blank lines and normalise whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def clean_article(article: dict) -> dict:
    wikitext = article.get("wikitext", "")
    return {
        "pageid": article["pageid"],
        "title": article["title"],
        "url": article["url"],
        "categories": article.get("categories", []),
        "last_edited": article.get("last_edited", ""),
        "is_redirect": _is_redirect(wikitext),
        "has_infobox": bool(re.search(r'\{\{\s*Infobox', wikitext, re.IGNORECASE)),
        "infobox_type": _infobox_name(wikitext),
        "text": clean_wikitext(wikitext),
    }


def run(input_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(input_dir.glob("*.json"))
    total = len(files)
    done = errors = skipped = 0

    for i, path in enumerate(files):
        out_path = output_dir / path.name
        if out_path.exists():
            skipped += 1
            continue
        try:
            article = json.loads(path.read_text())
            cleaned = clean_article(article)
            out_path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2))
            done += 1
        except Exception as exc:
            log.error("Failed %s: %s", path.name, exc)
            errors += 1

        if (i + 1) % 2000 == 0 or i + 1 == total:
            log.info("Progress: %d / %d | done %d | skipped %d | errors %d",
                     i + 1, total, done, skipped, errors)

    log.info("Done. %d cleaned, %d skipped, %d errors.", done, skipped, errors)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean wiki markup → plain text")
    parser.add_argument("--input-dir", default="data/articles", type=Path)
    parser.add_argument("--output-dir", default="data/cleaned", type=Path)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    run(args.input_dir, args.output_dir)


if __name__ == "__main__":
    main()
