#!/usr/bin/env python3
"""
Extract structured data from MediaWiki infobox templates.

Reads:  data/articles/{pageid}.json
Writes: data/structured/{pageid}.json  (typed structured record)

Re-running is safe: already-extracted files are skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

import mwparserfromhell

log = logging.getLogger(__name__)

DUTCH_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "augustus": 8,
    "september": 9, "oktober": 10, "november": 11, "december": 12,
}

_SKIP_NAMESPACES = frozenset([
    "category", "categorie", "bestand", "file", "image", "gallery",
])


# ── Field-level helpers ────────────────────────────────────────────────────────

def _strip(value: str) -> str:
    """Strip wiki markup from a single field value."""
    try:
        return mwparserfromhell.parse(value).strip_code(normalize=True).strip()
    except Exception:
        return value.strip()


def _split_list(value: str) -> list[str]:
    items = re.split(r'[,\n]+', value)
    return [s.strip() for s in items if s.strip()]


def _parse_date(raw: str) -> str | None:
    s = raw.strip()
    # "16 juli 1945"
    m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', s, re.IGNORECASE)
    if m:
        day, month_name, year = m.groups()
        month = DUTCH_MONTHS.get(month_name.lower())
        if month:
            return f"{year}-{month:02d}-{int(day):02d}"
    # bare year
    m = re.match(r'^(\d{4})$', s)
    if m:
        return m.group(1)
    return s or None


def _parse_period(raw: str) -> tuple[int | None, int | None]:
    """Parse "1974 - heden" or "1994-2004" → (start, end). end=None = still active."""
    years = re.findall(r'\b(1[89]\d{2}|20\d{2})\b', raw)
    if not years:
        return None, None
    start = int(years[0])
    end = int(years[1]) if len(years) > 1 else None
    if "heden" in raw.lower():
        end = None
    return start, end


def _extract_wikilinks(raw: str) -> list[str]:
    """Return display texts of [[wikilinks]] in a field value, skipping namespace links."""
    results: list[str] = []
    try:
        for link in mwparserfromhell.parse(raw).filter_wikilinks():
            target = str(link.title).strip()
            if ":" in target and target.split(":")[0].lower() in _SKIP_NAMESPACES:
                continue
            display = str(link.text).strip() if link.text else target
            display = mwparserfromhell.parse(display).strip_code().strip()
            if display:
                results.append(display)
    except Exception:
        pass
    return results


# ── Infobox extraction ─────────────────────────────────────────────────────────

def _extract_infobox(wikitext: str) -> tuple[str | None, dict[str, str]]:
    """Return (template_name, {field: raw_value}) for the first infobox found."""
    try:
        wikicode = mwparserfromhell.parse(wikitext)
        for tmpl in wikicode.filter_templates(recursive=False):
            name = tmpl.name.strip()
            if name.lower().startswith("infobox"):
                fields: dict[str, str] = {}
                for param in tmpl.params:
                    key = param.name.strip()
                    val = str(param.value).strip()
                    if val:
                        fields[key] = val
                return name, fields
    except Exception:
        pass
    return None, {}


def _detect_type(wikitext: str, infobox: str | None, categories: list[str]) -> str:
    first = wikitext.strip()[:20].lower()
    if first.startswith(("#redirect", "#doorverwijzing")):
        return "redirect"
    if infobox:
        il = infobox.lower()
        if "persoon" in il:
            return "persoon"
        if "productie" in il:
            return "productie"
        if "omroep" in il:
            return "omroep"
        if "producent" in il:
            return "producent_bedrijf"
        return "other"
    cats = [c.lower() for c in categories]
    if any("personen" in c for c in cats):
        return "persoon"
    if any("producties" in c or c == "productie" for c in cats):
        return "productie"
    if any("genres" in c or c == "genre" for c in cats):
        return "genre"
    if any("onderwerpen" in c or c == "onderwerp" for c in cats):
        return "onderwerp"
    return "other"


# ── Type-specific builders ─────────────────────────────────────────────────────

def _build_persoon(fields: dict[str, str]) -> dict:
    period_start, period_end = _parse_period(_strip(fields.get("periode_actief", "")))
    functies_raw = _strip(fields.get("functies", ""))
    return {
        "naam": _strip(fields.get("naam", "")),
        "birth_date": _parse_date(_strip(fields.get("geboorte_datum", ""))),
        "birth_place": _strip(fields.get("geboorte_plaats", "")),
        "death_date": _parse_date(_strip(fields.get("overlijden_datum", ""))),
        "death_place": _strip(fields.get("overlijden_plaats", "")),
        "functions": _split_list(functies_raw) if functies_raw else [],
        "period_start": period_start,
        "period_end": period_end,
        "collaborators": _extract_wikilinks(fields.get("werkt_samen_met", "")),
        "known_for": _extract_wikilinks(fields.get("bekend_van", "")),
    }


def _makers_persons(wikitext: str) -> list[str]:
    """Extract linked names from the === Makers === narrative section."""
    m = re.search(r'===\s*Makers\s*===\s*\n(.*?)(?====|\Z)', wikitext, re.DOTALL)
    if not m:
        return []
    persons: list[str] = []
    for hit in re.finditer(r'\[\[([^\]|#]+?)(?:\|[^\]]*)?\]\]', m.group(1)):
        target = hit.group(1).strip()
        if ":" not in target and target:
            persons.append(target)
    return persons


def _build_productie(fields: dict[str, str], wikitext: str) -> dict:
    period_start, period_end = _parse_period(_strip(fields.get("periode", "")))
    # Single-year production: period_end = period_start
    if period_start is not None and period_end is None:
        # Only set equal if "heden" not present (still-running production)
        if "heden" not in fields.get("periode", "").lower():
            period_end = period_start
    genre_raw = _strip(fields.get("genre", ""))
    return {
        "genre": _split_list(genre_raw) if genre_raw else [],
        "medium": _strip(fields.get("medium", "")).lower(),
        "period_start": period_start,
        "period_end": period_end,
        "persons": _makers_persons(wikitext),
    }


# ── Main extraction ────────────────────────────────────────────────────────────

def extract_structured(article: dict) -> dict:
    wikitext = article.get("wikitext", "")
    categories = article.get("categories", [])
    infobox_name, fields = _extract_infobox(wikitext)
    article_type = _detect_type(wikitext, infobox_name, categories)

    result: dict = {
        "pageid": article["pageid"],
        "title": article["title"],
        "url": article["url"],
        "categories": categories,
        "last_edited": article.get("last_edited", ""),
        "article_type": article_type,
        "infobox_type": infobox_name,
    }

    if article_type == "persoon" and fields:
        result["persoon"] = _build_persoon(fields)
    elif article_type == "productie":
        result["productie"] = _build_productie(fields, wikitext)

    return result


def run(input_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(input_dir.glob("*.json"))
    total = len(files)
    done = errors = skipped = 0
    type_counts: dict[str, int] = {}

    for i, path in enumerate(files):
        out_path = output_dir / path.name
        if out_path.exists():
            skipped += 1
            continue
        try:
            article = json.loads(path.read_text())
            record = extract_structured(article)
            out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            t = record["article_type"]
            type_counts[t] = type_counts.get(t, 0) + 1
            done += 1
        except Exception as exc:
            log.error("Failed %s: %s", path.name, exc)
            errors += 1

        if (i + 1) % 2000 == 0 or i + 1 == total:
            log.info("Progress: %d / %d | done %d | skipped %d | errors %d",
                     i + 1, total, done, skipped, errors)

    log.info("Done. %d extracted, %d skipped, %d errors.", done, skipped, errors)
    log.info("Article types: %s", type_counts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract infobox fields → structured records")
    parser.add_argument("--input-dir", default="data/articles", type=Path)
    parser.add_argument("--output-dir", default="data/structured", type=Path)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    run(args.input_dir, args.output_dir)


if __name__ == "__main__":
    main()
