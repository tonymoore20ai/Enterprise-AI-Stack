"""
Weather-safety knowledge base: markdown files in knowledge/, chunked by section,
embedded on the CPU with fastembed, stored in Qdrant.

  from knowledge import search
  search("when can I go back outside after thunder?")  # -> top matching sections

Loaded by ingest_knowledge.py (writes) and by the agent's search_knowledge tool (reads).
"""
import os
import re
import uuid
from functools import lru_cache
from pathlib import Path

COLLECTION = os.environ.get("KNOWLEDGE_COLLECTION", "weather-safety")
EMBED_MODEL = os.environ.get("KNOWLEDGE_EMBED_MODEL", "BAAI/bge-small-en-v1.5")  # 384 dims, CPU-friendly
QDRANT_HOST = os.environ.get("QDRANT_HOST", "vector-db")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
MIN_SCORE = float(os.environ.get("KNOWLEDGE_MIN_SCORE", "0.5"))  # below this a hit is treated as unrelated


@lru_cache(maxsize=1)
def embedder():
    from fastembed import TextEmbedding  # comes with nemoguardrails; heavy import, so lazy
    return TextEmbedding(model_name=EMBED_MODEL, cache_dir=os.environ.get("FASTEMBED_CACHE_PATH"))


@lru_cache(maxsize=1)
def client():
    import warnings
    from qdrant_client import QdrantClient
    # Plain HTTP is expected: Qdrant is only reachable on the internal Docker network
    warnings.filterwarnings("ignore", message="Api key is used with an insecure connection")
    return QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, https=False,
                        api_key=os.environ.get("QDRANT_API_KEY") or None)  # from .env via compose


def embed_passages(texts):
    return [v.tolist() for v in embedder().passage_embed(list(texts))]


def embed_query(text):
    return next(iter(embedder().query_embed(text))).tolist()


def chunk_markdown(path: Path):
    """One chunk per '## ' section. The document title and the section heading are
    kept in the embedded text, so a section still makes sense on its own."""
    text = path.read_text(encoding="utf-8")
    title_m = re.search(r"^# (.+)$", text, re.M)
    title = title_m.group(1).strip() if title_m else path.stem
    sources = re.findall(r"^Source:\s*(\S+)\s*$", text, re.M)
    chunks = []
    for part in re.split(r"^## ", text, flags=re.M)[1:]:
        heading, _, body = part.partition("\n")
        body = body.strip()
        if not body:
            continue
        chunks.append({
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{path.name}#{heading.strip()}")),
            "doc": path.name,
            "title": title,
            "section": heading.strip(),
            "text": body,
            "sources": sources,
            "embed_text": f"{title} - {heading.strip()}\n{body}",
        })
    return chunks


def search(query: str, k: int = 3):
    """Top-k sections for a question, best first. Hits below MIN_SCORE are dropped,
    so an unrelated question returns [] instead of loosely related advice."""
    hits = client().query_points(
        collection_name=COLLECTION, query=embed_query(query), limit=k, with_payload=True,
    ).points
    return [
        {
            "doc": h.payload["doc"],
            "title": h.payload["title"],
            "section": h.payload["section"],
            "text": h.payload["text"],
            "source": (h.payload.get("sources") or [""])[0],
            "score": round(h.score, 3),
        }
        for h in hits
        if h.score >= MIN_SCORE
    ]
