"""
Load the weather-safety documents in knowledge/*.md into Qdrant.
Safe to re-run: the collection is rebuilt from the files each time, so edits,
new files and deleted files are all picked up.

  docker exec agent-orchestrator python ingest_knowledge.py
  docker exec agent-orchestrator python ingest_knowledge.py --test "is it safe to swim during a thunderstorm?"

The first run downloads the embedding model (~130 MB) into the fastembed_cache volume.
"""
import argparse
import sys
import time

from qdrant_client import models

import knowledge as kb


def ingest():
    files = sorted(kb.KNOWLEDGE_DIR.glob("*.md"))
    if not files:
        sys.exit(f"[!] No .md files in {kb.KNOWLEDGE_DIR}")
    chunks = [c for f in files for c in kb.chunk_markdown(f)]
    print(f"[*] {len(files)} documents -> {len(chunks)} sections")

    start = time.perf_counter()
    vectors = kb.embed_passages(c["embed_text"] for c in chunks)
    print(f"[*] Embedded with {kb.EMBED_MODEL} in {time.perf_counter() - start:.1f}s "
          f"({len(vectors[0])} dims)")

    qc = kb.client()
    if qc.collection_exists(kb.COLLECTION):
        qc.delete_collection(kb.COLLECTION)
    qc.create_collection(
        collection_name=kb.COLLECTION,
        vectors_config=models.VectorParams(size=len(vectors[0]), distance=models.Distance.COSINE),
    )
    qc.upsert(
        collection_name=kb.COLLECTION,
        points=[
            models.PointStruct(
                id=c["id"], vector=v,
                payload={k: c[k] for k in ("doc", "title", "section", "text", "sources")},
            )
            for c, v in zip(chunks, vectors)
        ],
        wait=True,
    )
    count = qc.count(kb.COLLECTION, exact=True).count
    print(f"[*] Collection '{kb.COLLECTION}' now holds {count} sections.")


def test(query):
    print(f"\nQuery: {query}")
    hits = kb.search(query)
    if not hits:
        print(f"  (no section scored above {kb.MIN_SCORE})")
    for h in hits:
        print(f"  {h['score']:.3f}  {h['title']} / {h['section']}")


def main():
    parser = argparse.ArgumentParser(description="Load knowledge/*.md into Qdrant")
    parser.add_argument("--test", metavar="QUESTION", help="only run a search, don't re-ingest")
    args = parser.parse_args()
    if args.test:
        test(args.test)
        return
    ingest()
    for q in ("When is it safe to go outside after hearing thunder?",
              "Can I drive through a flooded road?",
              "What are the signs of heat stroke?"):
        test(q)


if __name__ == "__main__":
    main()
