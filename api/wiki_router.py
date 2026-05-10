"""
Structured query routing for the wiki agent's dual-path retrieval.

Given a natural language question, selects which named SPARQL queries to run
and extracts the required parameters from the question text.

Handles the most common structured question patterns:
  - Period queries (persons or productions active/airing between year X and Y)
  - Genre queries (productions of a specific genre)
  - Medium queries (TV / Radio / Film productions)
  - Function queries (persons by role: presentator, acteur, etc.)
  - Broadcaster queries (list of omroepen)

Questions that don't match any pattern fall back to semantic search only.
"""

from __future__ import annotations

import re


# ── Decade lookup ──────────────────────────────────────────────────────────────

_DECADES_NL = {
    "twintig": (1920, 1929),
    "dertig": (1930, 1939),
    "veertig": (1940, 1949),
    "vijftig": (1950, 1959),
    "zestig": (1960, 1969),
    "zeventig": (1970, 1979),
    "tachtig": (1980, 1989),
    "negentig": (1990, 1999),
}

_DECADES_EN = {
    "1920s": (1920, 1929), "20s": (1920, 1929),
    "1930s": (1930, 1939), "30s": (1930, 1939),
    "1940s": (1940, 1949), "40s": (1940, 1949),
    "1950s": (1950, 1959), "50s": (1950, 1959),
    "1960s": (1960, 1969), "60s": (1960, 1969),
    "1970s": (1970, 1979), "70s": (1970, 1979),
    "1980s": (1980, 1989), "80s": (1980, 1989),
    "1990s": (1990, 1999), "90s": (1990, 1999),
}

_MEDIUM_MAP = {
    "televisie": "Televisie", "television": "Televisie",
    " tv ": "Televisie", "tv-": "Televisie",
    "radio": "Radio",
    "film": "Film", "cinema": "Film",
}

# Common function values in the wiki — maps question keywords to SPARQL param
_FUNCTION_MAP = {
    "presentator": "Presentator", "presentatrice": "Presentator",
    "presenter": "Presentator", "host": "Presentator",
    "acteur": "Acteur", "actor": "Acteur", "actrice": "Acteur",
    "regisseur": "Regisseur", "director": "Regisseur",
    "journalist": "Journalist", "verslaggever": "Journalist",
    "zanger": "Zanger", "zangeres": "Zanger", "singer": "Zanger",
    "schrijver": "Schrijver", "writer": "Schrijver",
    "comedian": "Cabaretier", "cabaretier": "Cabaretier",
    "producer": "Producer",
}

_BROADCASTER_SIGNALS = {
    "broadcaster", "omroep", "broadcasting", "omroepen",
    "broadcasters", "zenders", "stations",
}

_PERSON_PERIOD_SIGNALS = {
    "actief", "active", "werkzaam", "werkten", "werkte",
    "wie was", "who was", "wie waren", "who were",
    "personen", "persons", "mensen", "people",
}

_PRODUCTION_PERIOD_SIGNALS = {
    "productie", "producties", "production", "productions",
    "programma", "programma's", "programme", "programmes",
    "serie", "series", "show", "shows",
    "televisieshow", "uitzending",
}


def _extract_years(question: str) -> tuple[int | None, int | None]:
    """Extract start and end year from a question string."""
    years = re.findall(r'\b(1[5-9]\d{2}|20[0-9]{2})\b', question)
    if len(years) >= 2:
        return int(years[0]), int(years[1])
    if len(years) == 1:
        # Single year — use it as both start and end (±0)
        return int(years[0]), int(years[0])

    q_lower = question.lower()
    # Dutch decade phrases: "jaren zeventig", "jaren tachtig"
    for label, (start, end) in _DECADES_NL.items():
        if label in q_lower:
            return start, end
    # English decade phrases: "1970s", "the 70s"
    for label, (start, end) in _DECADES_EN.items():
        if label in q_lower:
            return start, end

    return None, None


def _detect_medium(question: str) -> str | None:
    q_lower = " " + question.lower() + " "
    for kw, medium in _MEDIUM_MAP.items():
        if kw in q_lower:
            return medium
    return None


def _detect_function(question: str) -> str | None:
    q_lower = question.lower()
    for kw, fn in _FUNCTION_MAP.items():
        if kw in q_lower:
            return fn
    return None


def select(question: str) -> list[tuple[str, dict]]:
    """
    Return a list of (query_name, params) to run against Fuseki for this question.
    Returns [] when no structured query pattern is detected.
    """
    selections: list[tuple[str, dict]] = []
    q_lower = question.lower()

    # Period detection
    start_year, end_year = _extract_years(question)
    if start_year is not None:
        end_year = end_year or start_year
        has_person_signal = any(s in q_lower for s in _PERSON_PERIOD_SIGNALS)
        has_production_signal = any(s in q_lower for s in _PRODUCTION_PERIOD_SIGNALS)

        if has_person_signal:
            selections.append(("persons_active_in_period", {
                "start_year": start_year, "end_year": end_year
            }))
        if has_production_signal:
            selections.append(("productions_in_period", {
                "start_year": start_year, "end_year": end_year
            }))
        if not has_person_signal and not has_production_signal:
            # Ambiguous — run both, limit to persons first
            selections.append(("persons_active_in_period", {
                "start_year": start_year, "end_year": end_year
            }))

    # Function / role detection
    fn = _detect_function(question)
    if fn:
        selections.append(("persons_by_function", {"function": fn}))

    # Medium detection (only if asking about productions)
    medium = _detect_medium(question)
    if medium:
        has_production_context = any(s in q_lower for s in _PRODUCTION_PERIOD_SIGNALS | {
            "welke", "which", "what", "lijst", "list",
        })
        if has_production_context:
            selections.append(("productions_by_medium", {"medium": medium}))

    # Broadcaster detection
    if any(s in q_lower for s in _BROADCASTER_SIGNALS):
        selections.append(("all_broadcasters", {}))

    return selections


# ── SPARQL result formatters ───────────────────────────────────────────────────

def format_sparql_results(query_name: str, rows: list[dict]) -> str:
    """Format SPARQL result rows as a readable text block for the LLM."""
    if not rows:
        return ""

    formatters = {
        "persons_active_in_period": _fmt_persons_period,
        "productions_in_period":    _fmt_productions_period,
        "persons_by_function":      _fmt_persons_function,
        "productions_by_genre":     _fmt_productions_genre,
        "productions_by_medium":    _fmt_productions_medium,
        "all_broadcasters":         _fmt_broadcasters,
    }
    fn = formatters.get(query_name, _fmt_generic)
    return fn(rows)


_STRUCTURED_HEADER = "[Gestructureerde data — volledige lijst uit de Beeld & Geluid Wiki kennisgraaf. Presenteer deze lijst volledig en voeg geen informatie toe uit andere bronnen.]"


def _fmt_persons_period(rows: list[dict]) -> str:
    if not rows:
        return ""
    sample = rows[:50]
    lines = [
        _STRUCTURED_HEADER,
        f"Personen actief in deze periode ({len(rows)} gevonden, {len(sample)} getoond):",
    ]
    for r in sample:
        name = r.get("name", "?")
        start = r.get("start", "?")
        end = r.get("end", "heden")
        lines.append(f"- {name} (actief: {start}–{end})")
    if len(rows) > len(sample):
        lines.append(f"... en {len(rows) - len(sample)} meer.")
    return "\n".join(lines)


def _fmt_productions_period(rows: list[dict]) -> str:
    if not rows:
        return ""
    sample = rows[:50]
    lines = [
        _STRUCTURED_HEADER,
        f"Producties in deze periode ({len(rows)} gevonden, {len(sample)} getoond):",
    ]
    for r in sample:
        name = r.get("name", "?")
        start = r.get("start", "?")
        end = r.get("end", "")
        medium = r.get("medium", "")
        line = f"- {name}"
        if start:
            line += f" ({start}–{end})" if end else f" ({start}–)"
        if medium:
            line += f" [{medium}]"
        lines.append(line)
    if len(rows) > len(sample):
        lines.append(f"... en {len(rows) - len(sample)} meer.")
    return "\n".join(lines)


def _fmt_persons_function(rows: list[dict]) -> str:
    if not rows:
        return ""
    function = rows[0].get("function", "deze rol") if rows else "deze rol"
    sample = rows[:50]
    lines = [
        _STRUCTURED_HEADER,
        f"Personen met functie '{function}' ({len(rows)} gevonden, {len(sample)} getoond):",
    ]
    for r in sample:
        lines.append(f"- {r.get('name', '?')}")
    if len(rows) > len(sample):
        lines.append(f"... en {len(rows) - len(sample)} meer.")
    return "\n".join(lines)


def _fmt_productions_genre(rows: list[dict]) -> str:
    if not rows:
        return ""
    genre = rows[0].get("genre", "dit genre") if rows else "dit genre"
    sample = rows[:50]
    lines = [
        _STRUCTURED_HEADER,
        f"Producties in genre '{genre}' ({len(rows)} gevonden, {len(sample)} getoond):",
    ]
    for r in sample:
        name = r.get("name", "?")
        start = r.get("start", "")
        end = r.get("end", "")
        line = f"- {name}"
        if start:
            line += f" ({start}–{end})" if end else f" ({start}–)"
        lines.append(line)
    if len(rows) > len(sample):
        lines.append(f"... en {len(rows) - len(sample)} meer.")
    return "\n".join(lines)


def _fmt_productions_medium(rows: list[dict]) -> str:
    if not rows:
        return ""
    medium = rows[0].get("medium", "") if rows else ""
    sample = rows[:50]
    lines = [
        _STRUCTURED_HEADER,
        f"{medium}-producties ({len(rows)} gevonden, {len(sample)} getoond):",
    ]
    for r in sample:
        name = r.get("name", "?")
        start = r.get("start", "")
        end = r.get("end", "")
        line = f"- {name}"
        if start:
            line += f" ({start}–{end})" if end else f" ({start}–)"
        lines.append(line)
    if len(rows) > len(sample):
        lines.append(f"... en {len(rows) - len(sample)} meer.")
    return "\n".join(lines)


def _fmt_broadcasters(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = [
        _STRUCTURED_HEADER,
        f"Nederlandse omroepen in de wiki ({len(rows)}):",
    ]
    for r in rows:
        lines.append(f"- {r.get('name', '?')}")
    return "\n".join(lines)


def _fmt_generic(rows: list[dict]) -> str:
    lines = [_STRUCTURED_HEADER]
    for r in rows[:20]:
        lines.append(", ".join(f"{k}: {v}" for k, v in r.items()))
    return "\n".join(lines)
