#!/usr/bin/env python3
"""
Embed chunks into a Milvus collection.

Reads:  data/chunks/{pageid}.json
Writes: Milvus collection "mediasuite_wiki"

Local setup (proof of concept):
  - Milvus Lite: URI is a local file path, e.g. data/milvus_wiki.db
  - Embedding model: intfloat/multilingual-e5-large-instruct via sentence-transformers

Scaling up:
  - Replace MILVUS_URI with a real Milvus/Zilliz endpoint, e.g. http://milvus:19530
  - Replace LocalEmbedder with an HTTP client to the mediasuite-agent embedding API

Re-running is safe: already-embedded pageids are tracked and skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Protocol

from pymilvus import MilvusClient, DataType

log = logging.getLogger(__name__)

COLLECTION_NAME = "mediasuite_wiki"
EMBEDDING_DIM = 1024  # multilingual-e5-large-instruct output dimension


# ── Embedder interface ─────────────────────────────────────────────────────────
# Swap LocalEmbedder for any class implementing this protocol to change backend.

class Embedder(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class LocalEmbedder:
    """Runs multilingual-e5-large-instruct locally via sentence-transformers."""

    MODEL_NAME = "intfloat/multilingual-e5-large-instruct"

    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer
        log.info("Loading embedding model %s …", self.MODEL_NAME)
        self._model = SentenceTransformer(self.MODEL_NAME)
        log.info("Model loaded.")

    def encode(self, texts: list[str]) -> list[list[float]]:
        # Prefix required by multilingual-e5 for passage encoding
        prefixed = [f"passage: {t}" for t in texts]
        vecs = self._model.encode(prefixed, normalize_embeddings=True, show_progress_bar=False)
        return vecs.tolist()


# ── Milvus collection setup ────────────────────────────────────────────────────

def _create_collection(client: MilvusClient) -> None:
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id",     DataType.VARCHAR, max_length=64, is_primary=True)
    schema.add_field("pageid",       DataType.INT64)
    schema.add_field("title",        DataType.VARCHAR, max_length=512)
    schema.add_field("url",          DataType.VARCHAR, max_length=512)
    schema.add_field("article_type", DataType.VARCHAR, max_length=32)
    schema.add_field("categories",   DataType.VARCHAR, max_length=1024)  # JSON array as string
    schema.add_field("last_edited",  DataType.VARCHAR, max_length=32)
    schema.add_field("chunk_text",   DataType.VARCHAR, max_length=2048)
    schema.add_field("chunk_index",  DataType.INT32)
    schema.add_field("is_infobox",   DataType.BOOL)
    schema.add_field("gtaa_uri",     DataType.VARCHAR, max_length=256)
    schema.add_field("embedding",    DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM)

    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="embedding",
        index_type="FLAT",   # exact search — fine for proof-of-concept
        metric_type="COSINE",
    )

    client.create_collection(
        collection_name=COLLECTION_NAME,
        schema=schema,
        index_params=index_params,
    )
    log.info("Created collection %r.", COLLECTION_NAME)


def get_client(uri: str) -> MilvusClient:
    client = MilvusClient(uri=uri)
    if not client.has_collection(COLLECTION_NAME):
        _create_collection(client)
    return client


# ── Already-embedded tracking ──────────────────────────────────────────────────

def _embedded_pageids(client: MilvusClient) -> set[int]:
    """Return the set of pageids already present in the collection."""
    try:
        results = client.query(
            collection_name=COLLECTION_NAME,
            filter="chunk_index == 0",
            output_fields=["pageid"],
            limit=16384,
        )
        return {r["pageid"] for r in results}
    except Exception:
        return set()


# ── Main embedding loop ────────────────────────────────────────────────────────

def run(
    chunks_dir: Path,
    milvus_uri: str,
    batch_size: int,
    embedder: Embedder,
) -> None:
    client = get_client(milvus_uri)
    already_done = _embedded_pageids(client)
    log.info("%d pageids already in Milvus.", len(already_done))

    files = [
        p for p in sorted(chunks_dir.glob("*.json"))
        if int(p.stem) not in already_done
    ]
    log.info("%d chunk files to embed.", len(files))

    batch: list[dict] = []
    inserted = errors = 0

    def flush(b: list[dict]) -> None:
        nonlocal inserted, errors
        texts = [c["chunk_text"] for c in b]
        try:
            embeddings = embedder.encode(texts)
            rows = [
                {
                    "chunk_id":     c["chunk_id"],
                    "pageid":       c["pageid"],
                    "title":        c["title"][:512],
                    "url":          c["url"][:512],
                    "article_type": c.get("article_type", "other")[:32],
                    "categories":   json.dumps(c.get("categories", []), ensure_ascii=False)[:1024],
                    "last_edited":  c.get("last_edited", "")[:32],
                    "chunk_text":   c["chunk_text"][:2048],
                    "chunk_index":  c["chunk_index"],
                    "is_infobox":   c.get("is_infobox", False),
                    "gtaa_uri":     (c.get("gtaa_uri") or "")[:256],
                    "embedding":    embeddings[i],
                }
                for i, c in enumerate(b)
            ]
            client.insert(collection_name=COLLECTION_NAME, data=rows)
            inserted += len(rows)
        except Exception as exc:
            log.error("Batch insert failed: %s", exc)
            errors += len(b)

    for i, path in enumerate(files):
        try:
            chunks = json.loads(path.read_text())
        except Exception as exc:
            log.error("Failed to read %s: %s", path.name, exc)
            errors += 1
            continue

        for chunk in chunks:
            batch.append(chunk)
            if len(batch) >= batch_size:
                flush(batch)
                batch = []

        if (i + 1) % 500 == 0 or i + 1 == len(files):
            log.info("Progress: %d / %d files | %d chunks inserted | %d errors",
                     i + 1, len(files), inserted, errors)

    if batch:
        flush(batch)

    log.info("Done. %d chunks inserted, %d errors.", inserted, errors)


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed chunks into Milvus")
    parser.add_argument("--chunks-dir", default="data/chunks", type=Path)
    parser.add_argument(
        "--milvus-uri", default="data/milvus_wiki.db",
        help="Milvus URI. Local file for Milvus Lite (default), "
             "or http://host:19530 for a real Milvus instance.",
    )
    parser.add_argument(
        "--batch-size", default=64, type=int,
        help="Chunks per embedding + insert batch (default: 64)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    embedder = LocalEmbedder()
    run(args.chunks_dir, args.milvus_uri, args.batch_size, embedder)


if __name__ == "__main__":
    main()
