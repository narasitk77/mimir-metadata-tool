import logging
import uuid
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointIdsList,
    PointStruct,
    VectorParams,
)

from app.config import settings

logger = logging.getLogger(__name__)

VECTOR_DIM = 384
_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

_client: Optional[QdrantClient] = None
_model = None  # fastembed.TextEmbedding — lazy loaded on first use


def _point_id(item_id: str) -> str:
    """Deterministic UUID from Mimir item_id."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, item_id))


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=settings.QDRANT_URL, timeout=10)
    return _client


def get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        logger.info(f"Loading embedding model: {_MODEL_NAME}")
        _model = TextEmbedding(model_name=_MODEL_NAME, cache_dir="/app/data/models")
        logger.info("Embedding model ready")
    return _model


def init_collection() -> None:
    """Create Qdrant collection if it doesn't exist. Called once at startup."""
    client = get_client()
    existing = {c.name for c in client.get_collections().collections}
    if settings.QDRANT_COLLECTION not in existing:
        client.create_collection(
            collection_name=settings.QDRANT_COLLECTION,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )
        logger.info(f"Created Qdrant collection: {settings.QDRANT_COLLECTION}")
    else:
        logger.info(f"Qdrant collection ready: {settings.QDRANT_COLLECTION}")


def _build_text(asset) -> str:
    """Build searchable text from all relevant asset fields."""
    parts = [
        asset.ai_title or asset.title or "",
        asset.ai_description or "",
        asset.ai_keyword or "",
        asset.ai_persons or "",
        asset.ai_location or "",
        asset.ai_category or "",
        asset.ai_subcat or "",
        asset.ai_editorial_categories or "",
        asset.ai_subject_tags or "",
        asset.ai_event_occasion or "",
        asset.ai_project_series or "",
        asset.exif_photographer or "",
    ]
    return " ".join(p for p in parts if p).strip()


def index_asset(asset) -> bool:
    """Embed and upsert a single asset into Qdrant. Returns True if indexed."""
    text = _build_text(asset)
    if not text:
        return False

    model = get_model()
    vector = list(model.embed([text]))[0].tolist()

    get_client().upsert(
        collection_name=settings.QDRANT_COLLECTION,
        points=[
            PointStruct(
                id=_point_id(asset.item_id),
                vector=vector,
                payload={
                    "item_id": asset.item_id,
                    "title": asset.ai_title or asset.title or "",
                    "thumbnail_url": asset.thumbnail_url or "",
                    "item_type": asset.item_type or "",
                    "media_created_on": asset.media_created_on or "",
                    "ai_persons": asset.ai_persons or "",
                    "ai_location": asset.ai_location or "",
                },
            )
        ],
    )
    return True


def search(query: str, limit: int = 20, item_type: Optional[str] = None) -> list[dict]:
    """Semantic search — returns list of {item_id, score, title, ...}."""
    model = get_model()
    vector = list(model.embed([query]))[0].tolist()

    query_filter = None
    if item_type:
        query_filter = Filter(
            must=[FieldCondition(key="item_type", match=MatchValue(value=item_type))]
        )

    results = get_client().search(
        collection_name=settings.QDRANT_COLLECTION,
        query_vector=vector,
        limit=limit,
        query_filter=query_filter,
        with_payload=True,
    )
    return [
        {
            "item_id": r.payload["item_id"],
            "score": round(r.score, 4),
            "title": r.payload.get("title", ""),
            "thumbnail_url": r.payload.get("thumbnail_url", ""),
            "item_type": r.payload.get("item_type", ""),
            "media_created_on": r.payload.get("media_created_on", ""),
            "ai_persons": r.payload.get("ai_persons", ""),
            "ai_location": r.payload.get("ai_location", ""),
        }
        for r in results
    ]


def delete_asset(item_id: str) -> None:
    """Remove an asset from the vector index."""
    get_client().delete(
        collection_name=settings.QDRANT_COLLECTION,
        points_selector=PointIdsList(points=[_point_id(item_id)]),
    )


def collection_info() -> dict:
    """Return basic stats about the vector collection."""
    try:
        info = get_client().get_collection(settings.QDRANT_COLLECTION)
        return {
            "vectors_count": info.vectors_count or 0,
            "points_count": info.points_count or 0,
            "status": str(info.status),
            "collection": settings.QDRANT_COLLECTION,
            "model": _MODEL_NAME,
            "vector_dim": VECTOR_DIM,
        }
    except Exception as e:
        return {
            "vectors_count": 0,
            "points_count": 0,
            "status": "unavailable",
            "error": str(e),
        }
