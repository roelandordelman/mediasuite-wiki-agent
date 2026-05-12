#!/usr/bin/env python3.12
"""
Unit tests for api/wiki_router.py — no running services required.

Tests the routing logic in isolation: given a natural language question,
does select() return the right (query_name, params) tuples?

Run:
  python3.12 evaluation/test_router.py
  python3.12 evaluation/test_router.py --verbose   # show all cases, not just failures

Exit code 0 = all pass, 1 = one or more failures.
"""

from __future__ import annotations

import sys
import argparse
from pathlib import Path
from dataclasses import dataclass, field

# Import wiki_router from the api/ directory without needing the repo on PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent / "api"))
from wiki_router import select, _extract_years  # noqa: E402


# ── Test case schema ───────────────────────────────────────────────────────────

@dataclass
class RouterCase:
    id: str
    question: str
    # List of expected (query_name, params) tuples.
    # Order does not matter. Use None for params to skip params check.
    expected: list[tuple[str, dict | None]]
    note: str = ""


@dataclass
class YearCase:
    id: str
    question: str
    expected_start: int | None
    expected_end: int | None
    note: str = ""


# ── Year extraction tests ──────────────────────────────────────────────────────

YEAR_CASES: list[YearCase] = [
    YearCase("y01", "Who was active between 1980 and 1990?", 1980, 1990),
    YearCase("y02", "Wie was actief tussen 1970 en 1980?", 1970, 1980),
    YearCase("y03", "Producties uit 1965", 1965, 1965,
             note="Single year → both start and end"),
    YearCase("y04", "Nederlandse televisie in de jaren zeventig", 1970, 1979,
             note="Dutch decade phrase"),
    YearCase("y05", "Dutch media in the 1980s", 1980, 1989,
             note="English decade phrase with full year"),
    YearCase("y06", "Wat was populair in the 80s?", 1980, 1989,
             note="English shorthand decade"),
    YearCase("y07", "Welke programma's waren er in de jaren tachtig?", 1980, 1989,
             note="Dutch decade — tachtig"),
    YearCase("y08", "Wie is Rob de Nijs?", None, None,
             note="No year — should return (None, None)"),
    YearCase("y09", "What happened in the jaren vijftig?", 1950, 1959,
             note="Dutch decade — vijftig"),
    YearCase("y10", "Radio in de jaren dertig", 1930, 1939,
             note="Dutch decade — dertig"),
]


# ── Routing tests ──────────────────────────────────────────────────────────────

ROUTER_CASES: list[RouterCase] = [

    # ── Period + person signal ─────────────────────────────────────────────────
    RouterCase(
        "r01",
        "Who was active in Dutch media between 1980 and 1990?",
        [("persons_active_in_period", {"start_year": 1980, "end_year": 1990})],
        note="The original failing question — 'active' is a person signal",
    ),
    RouterCase(
        "r02",
        "Wie was er actief tussen 1970 en 1980?",
        [("persons_active_in_period", {"start_year": 1970, "end_year": 1980})],
        note="Dutch: 'actief' + explicit years",
    ),
    RouterCase(
        "r03",
        "Welke personen waren werkzaam in de jaren tachtig?",
        [("persons_active_in_period", {"start_year": 1980, "end_year": 1989})],
        note="'personen' + 'werkzaam' + Dutch decade",
    ),
    RouterCase(
        "r04",
        "Who were active in the 1970s?",
        [("persons_active_in_period", {"start_year": 1970, "end_year": 1979})],
        note="English decade phrase",
    ),
    RouterCase(
        "r05",
        "Wie waren er werkzaam in de jaren vijftig?",
        [("persons_active_in_period", {"start_year": 1950, "end_year": 1959})],
        note="'werkzaam' + jaren vijftig",
    ),

    # ── Period + production signal ─────────────────────────────────────────────
    RouterCase(
        "r06",
        "Welke producties waren er in 1965?",
        [("productions_in_period", {"start_year": 1965, "end_year": 1965})],
        note="'producties' + single year",
    ),
    RouterCase(
        "r07",
        "What programmes aired in the 1980s?",
        [("productions_in_period", {"start_year": 1980, "end_year": 1989})],
        note="English: 'programmes' + English decade",
    ),
    RouterCase(
        "r08",
        "Welke programma's waren er in de jaren tachtig?",
        [("productions_in_period", {"start_year": 1980, "end_year": 1989})],
        note="Dutch: 'programma's' + jaren tachtig",
    ),

    # ── Period ambiguous (no person or production signal) → fallback to persons ─
    RouterCase(
        "r09",
        "What was happening in Dutch media in 1975?",
        [("persons_active_in_period", {"start_year": 1975, "end_year": 1975})],
        note="No person/production signal → ambiguous fallback = persons",
    ),

    # ── Period + both person and production signals ────────────────────────────
    RouterCase(
        "r10",
        "Welke personen en producties waren er tussen 1960 en 1970?",
        [
            ("persons_active_in_period", {"start_year": 1960, "end_year": 1970}),
            ("productions_in_period",    {"start_year": 1960, "end_year": 1970}),
        ],
        note="Both 'personen' and 'producties' → both queries fired",
    ),

    # ── Function routing ───────────────────────────────────────────────────────
    RouterCase(
        "r11",
        "Wie zijn de presentatoren in de wiki?",
        [("persons_by_function", {"function": "Presentator"})],
        note="'presentatoren' contains 'presentator'",
    ),
    RouterCase(
        "r12",
        "Geef een lijst van acteurs",
        [("persons_by_function", {"function": "Acteur"})],
        note="Dutch: 'acteurs' contains 'acteur'",
    ),
    RouterCase(
        "r13",
        "Who are the directors in the wiki?",
        [("persons_by_function", {"function": "Regisseur"})],
        note="English 'director' → Regisseur",
    ),
    RouterCase(
        "r14",
        "Geef me een lijst van zangers",
        [("persons_by_function", {"function": "Zanger"})],
        note="'zangers' contains 'zanger'",
    ),

    # ── Function + period combined ─────────────────────────────────────────────
    RouterCase(
        "r15",
        "Welke presentatoren waren actief in de jaren zeventig?",
        [
            ("persons_active_in_period", {"start_year": 1970, "end_year": 1979}),
            ("persons_by_function",      {"function": "Presentator"}),
        ],
        note="Period ('actief' + jaren zeventig) + function ('presentator') both fire",
    ),

    # ── Medium routing ─────────────────────────────────────────────────────────
    RouterCase(
        "r16",
        "Welke televisieproducties zijn er in de wiki?",
        [("productions_by_medium", {"medium": "Televisie"})],
        note="'televisie' + production context ('welke')",
    ),
    RouterCase(
        "r17",
        "What radio programmes are in the wiki?",
        [("productions_by_medium", {"medium": "Radio"})],
        note="'radio' + 'programmes' (production context)",
    ),
    RouterCase(
        "r18",
        "Geef een lijst van filmproducties",
        [("productions_by_medium", {"medium": "Film"})],
        note="'film' + 'lijst' (production context)",
    ),

    # ── Broadcaster routing ────────────────────────────────────────────────────
    RouterCase(
        "r19",
        "Welke omroepen zijn er?",
        [("all_broadcasters", {})],
        note="'omroepen' is a broadcaster signal",
    ),
    RouterCase(
        "r20",
        "Give me a list of Dutch broadcasters",
        [("all_broadcasters", {})],
        note="'broadcasters' is a broadcaster signal",
    ),
    RouterCase(
        "r21",
        "Hoeveel broadcasting stations kent de wiki?",
        [("all_broadcasters", {})],
        note="'broadcasting' + 'stations' are broadcaster signals",
    ),

    # ── No-match: should return [] ─────────────────────────────────────────────
    RouterCase(
        "r22",
        "Wie is Rob de Nijs?",
        [],
        note="Biographical question — no routing signals",
    ),
    RouterCase(
        "r23",
        "Wat is een praatprogramma?",
        [],
        note="Genre explanation — no routing signals",
    ),
    RouterCase(
        "r24",
        "Tell me about Mies Bouwman",
        [],
        note="Person biography in English — no routing signals",
    ),
    RouterCase(
        "r25",
        "How do I use the Media Suite?",
        [],
        note="Documentation question — no routing signals",
    ),
]


# ── Runner ─────────────────────────────────────────────────────────────────────

def _check_year_case(case: YearCase) -> tuple[bool, str]:
    start, end = _extract_years(case.question)
    if start == case.expected_start and end == case.expected_end:
        return True, ""
    return False, (
        f"expected start={case.expected_start} end={case.expected_end}, "
        f"got start={start} end={end}"
    )


def _check_router_case(case: RouterCase) -> tuple[bool, str]:
    selections = select(case.question)

    # Build comparable sets
    actual_names = {q for q, _ in selections}
    expected_names = {q for q, _ in case.expected}

    if actual_names != expected_names:
        return False, (
            f"wrong queries selected:\n"
            f"  expected: {sorted(expected_names) or '[]'}\n"
            f"  got:      {sorted(actual_names) or '[]'}"
        )

    # Check params for each expected query
    actual_map = dict(selections)
    for exp_name, exp_params in case.expected:
        if exp_params is None:
            continue  # skip param check
        act_params = actual_map.get(exp_name, {})
        mismatches = {
            k: (exp_params[k], act_params.get(k))
            for k in exp_params
            if act_params.get(k) != exp_params[k]
        }
        if mismatches:
            details = ", ".join(
                f"{k}: expected {exp!r} got {act!r}"
                for k, (exp, act) in mismatches.items()
            )
            return False, f"params mismatch for '{exp_name}': {details}"

    return True, ""


def run(verbose: bool = False) -> int:
    failures = 0
    total = 0

    print("── Year extraction ───────────────────────────────────────────")
    for case in YEAR_CASES:
        total += 1
        ok, msg = _check_year_case(case)
        tag = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
            print(f"  [{tag}] {case.id}: {case.question!r}")
            print(f"         {msg}")
            if case.note:
                print(f"         note: {case.note}")
        elif verbose:
            start, end = _extract_years(case.question)
            print(f"  [{tag}] {case.id}: {case.question!r}")
            print(f"         → ({start}, {end})")

    print()
    print("── Routing ───────────────────────────────────────────────────")
    for case in ROUTER_CASES:
        total += 1
        ok, msg = _check_router_case(case)
        tag = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
            print(f"  [{tag}] {case.id}: {case.question!r}")
            print(f"         {msg}")
            if case.note:
                print(f"         note: {case.note}")
        elif verbose:
            result = select(case.question)
            result_str = ", ".join(
                f"{q}({p})" for q, p in result
            ) or "(no match)"
            print(f"  [{tag}] {case.id}: {case.question!r}")
            print(f"         → {result_str}")

    print()
    passed = total - failures
    print(f"Results: {passed}/{total} passed", end="")
    if failures:
        print(f"  ({failures} failed)")
    else:
        print("  ✓")
    print()

    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true",
                        help="Show all cases, not just failures")
    args = parser.parse_args()
    sys.exit(run(verbose=args.verbose))
