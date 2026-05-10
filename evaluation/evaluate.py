#!/usr/bin/env python3
"""
Retrieval quality evaluation for the wiki agent.

Tests three retrieval paths:
  wiki_search  — semantic search (Hit@k, MRR)
  wiki_lookup  — exact title lookup (hit/miss)
  wiki_query   — named SPARQL queries (result count checks)

Usage:
  # Against the REST API (wiki server must be running on port 8002)
  python3.12 evaluation/evaluate.py --mode rest

  # Directly (no server needed; loads Milvus + embedder in-process)
  python3.12 evaluation/evaluate.py --mode direct

  # Verbose: show per-question results
  python3.12 evaluation/evaluate.py --mode rest --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
QUESTIONS_PATH = Path(__file__).parent / "test_questions.json"


# ── REST-mode client ───────────────────────────────────────────────────────────

def _rest_search(base_url: str, question: str, limit: int = 10) -> list[dict]:
    import requests
    r = requests.post(f"{base_url}/search", json={"query": question, "limit": limit}, timeout=30)
    r.raise_for_status()
    return r.json()


def _rest_lookup(base_url: str, title: str) -> dict | None:
    import requests
    r = requests.post(f"{base_url}/lookup", json={"title": title}, timeout=30)
    r.raise_for_status()
    return r.json()


def _rest_query(base_url: str, query_name: str, params: dict) -> list[dict]:
    import requests
    r = requests.post(f"{base_url}/query", json={"query_name": query_name, "params": params}, timeout=30)
    r.raise_for_status()
    return r.json()


# ── Direct-mode client ─────────────────────────────────────────────────────────

def _load_direct():
    sys.path.insert(0, str(ROOT))
    from mcp.server import wiki_search, wiki_lookup, wiki_query
    return wiki_search, wiki_lookup, wiki_query


# ── Metrics ────────────────────────────────────────────────────────────────────

def _hit_at_k(results: list[dict], expected_title: str, k: int) -> bool:
    """True if expected_title appears in the top-k result titles."""
    for r in results[:k]:
        if r.get("title", "").lower() == expected_title.lower():
            return True
    return False


def _hit_category_at_k(results: list[dict], expected_category: str, k: int) -> bool:
    """True if any result in top-k has the expected category."""
    for r in results[:k]:
        cats = r.get("categories", [])
        if isinstance(cats, str):
            import json as _json
            try:
                cats = _json.loads(cats)
            except Exception:
                cats = [cats]
        if any(expected_category.lower() in c.lower() for c in cats):
            return True
    return False


def _mrr(results: list[dict], expected_title: str) -> float:
    """Mean Reciprocal Rank (1/rank of first hit, 0 if not found)."""
    for i, r in enumerate(results, 1):
        if r.get("title", "").lower() == expected_title.lower():
            return 1.0 / i
    return 0.0


# ── Evaluation runners ─────────────────────────────────────────────────────────

def eval_search(questions: list[dict], search_fn, k: int = 5, verbose: bool = False) -> dict:
    hits = 0
    mrr_sum = 0.0
    total_with_title = 0
    total = len(questions)

    for q in questions:
        qid = q["id"]
        question = q["question"]
        results = search_fn(question, limit=k)

        if "expected_title" in q:
            total_with_title += 1
            hit = _hit_at_k(results, q["expected_title"], k)
            rr = _mrr(results, q["expected_title"])
            hits += int(hit)
            mrr_sum += rr
            status = "HIT" if hit else "MISS"
            if verbose:
                titles = [r.get("title", "?") for r in results[:k]]
                print(f"  [{qid}] {status}  rr={rr:.2f}  '{question}'")
                print(f"         expected: '{q['expected_title']}'")
                print(f"         got:      {titles}")
        elif "expected_category" in q:
            hit = _hit_category_at_k(results, q["expected_category"], k)
            hits += int(hit)
            total_with_title += 1
            status = "HIT" if hit else "MISS"
            if verbose:
                titles = [r.get("title", "?") for r in results[:k]]
                print(f"  [{qid}] {status}  '{question}'")
                print(f"         expected category: '{q['expected_category']}'")
                print(f"         got:               {titles}")
        else:
            if verbose:
                print(f"  [{qid}] SKIP (no expected_title or expected_category)  '{question}'")

    hit_rate = hits / total_with_title if total_with_title else 0.0
    mrr = mrr_sum / total_with_title if total_with_title else 0.0
    return {"total": total, "evaluated": total_with_title, "hits": hits,
            f"hit@{k}": round(hit_rate, 3), "mrr": round(mrr, 3)}


def eval_lookup(questions: list[dict], lookup_fn, verbose: bool = False) -> dict:
    correct = 0
    total = len(questions)

    for q in questions:
        qid = q["id"]
        title = q["title"]
        result = lookup_fn(title)
        found = result is not None
        expected_found = q.get("should_find", True)
        ok = found == expected_found

        correct += int(ok)
        if verbose:
            status = "OK" if ok else "FAIL"
            art_type = result.get("article_type", "?") if result else "None"
            print(f"  [{qid}] {status}  '{title}'  →  {art_type if found else 'null'}")

    return {"total": total, "correct": correct, "accuracy": round(correct / total, 3)}


def eval_queries(questions: list[dict], query_fn, verbose: bool = False) -> dict:
    passed = 0
    total = len(questions)

    for q in questions:
        qid = q["id"]
        query_name = q["query_name"]
        params = q.get("params", {})
        check = q.get("check", "len(results) >= 0")

        try:
            results = query_fn(query_name, params)
            ok = bool(eval(check, {"results": results}))
        except Exception as exc:
            ok = False
            if verbose:
                print(f"  [{qid}] ERROR  {query_name}({params}): {exc}")
            passed += 0
            continue

        passed += int(ok)
        if verbose:
            status = "PASS" if ok else "FAIL"
            print(f"  [{qid}] {status}  {query_name}({params})  → {len(results)} rows  check: {check}")

    return {"total": total, "passed": passed, "pass_rate": round(passed / total, 3)}


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate wiki agent retrieval quality")
    parser.add_argument("--mode", choices=["rest", "direct"], default="rest",
                        help="rest: call running wiki API; direct: import functions in-process")
    parser.add_argument("--url", default="http://localhost:8002",
                        help="Wiki REST API base URL (rest mode only)")
    parser.add_argument("--k", type=int, default=5, help="Hit@k for search evaluation")
    parser.add_argument("--verbose", action="store_true", help="Show per-question results")
    args = parser.parse_args()

    data = json.loads(QUESTIONS_PATH.read_text())

    if args.mode == "rest":
        base = args.url.rstrip("/")
        search_fn = lambda q, limit: _rest_search(base, q, limit)
        lookup_fn = lambda t: _rest_lookup(base, t)
        query_fn = lambda n, p: _rest_query(base, n, p)
    else:
        wiki_search, wiki_lookup, wiki_query = _load_direct()
        search_fn = wiki_search
        lookup_fn = wiki_lookup
        query_fn = wiki_query

    print(f"\n── wiki_search  (Hit@{args.k}, MRR) ──────────────────────────")
    if args.verbose:
        print()
    search_result = eval_search(data["wiki_search"], search_fn, k=args.k, verbose=args.verbose)
    print(f"  Hit@{args.k}:  {search_result[f'hit@{args.k}']:.1%}  ({search_result['hits']}/{search_result['evaluated']})")
    print(f"  MRR:    {search_result['mrr']:.3f}")

    print(f"\n── wiki_lookup  (accuracy) ──────────────────────────────────")
    if args.verbose:
        print()
    lookup_result = eval_lookup(data["wiki_lookup"], lookup_fn, verbose=args.verbose)
    print(f"  Accuracy: {lookup_result['accuracy']:.1%}  ({lookup_result['correct']}/{lookup_result['total']})")

    print(f"\n── wiki_query   (named SPARQL) ──────────────────────────────")
    if args.verbose:
        print()
    query_result = eval_queries(data["wiki_query"], query_fn, verbose=args.verbose)
    print(f"  Pass rate: {query_result['pass_rate']:.1%}  ({query_result['passed']}/{query_result['total']})")
    print()


if __name__ == "__main__":
    main()
