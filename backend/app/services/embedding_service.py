"""ChromaDB + sentence-transformers embedding service. Two collections:
- meeting_snippets: transcript lines + summary blob, feeds Ask Coco RAG
  (Task 5.5, app.services.askcoco_service)
- decisions: one embedding per extracted decision, feeds contradiction
  detection (Task 4.4, app.graph.contradiction_service)

Adapted from ASK COCO/server.py's init_memory()/load_all_meetings(), which
read straight from MEETINGS/results/*.json — this version is called directly
from the Celery pipeline with already-in-memory data instead of re-reading
from disk, and is a reusable service instead of module-level globals.
"""
import hashlib
import logging

import chromadb
from chromadb.utils import embedding_functions

from app.core.config import get_settings
from app.schemas.meeting_intelligence import MeetingIntelligence

logger = logging.getLogger(__name__)
settings = get_settings()

_client = None
_embedding_fn = None
_snippets_collection = None
_decisions_collection = None


def _get_client():
    global _client, _embedding_fn
    if _client is None:
        _client = chromadb.PersistentClient(path=settings.chroma_path)
        try:
            _embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="all-MiniLM-L6-v2"
            )
        except Exception as e:
            logger.warning(f"Failed to load embedding function: {e}. Using ChromaDB default.")
            _embedding_fn = None
    return _client


def _get_collection(name: str):
    client = _get_client()
    if _embedding_fn:
        return client.get_or_create_collection(name=name, embedding_function=_embedding_fn)
    return client.get_or_create_collection(name=name)


def get_snippets_collection():
    global _snippets_collection
    if _snippets_collection is None:
        _snippets_collection = _get_collection("meeting_snippets")
    return _snippets_collection


def get_decisions_collection():
    global _decisions_collection
    if _decisions_collection is None:
        _decisions_collection = _get_collection("decisions")
    return _decisions_collection


def _stable_hash(text: str) -> str:
    """Deterministic short id — Python's built-in hash() is randomized per
    process (PYTHONHASHSEED), which would duplicate entries in Chroma on
    every worker restart."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def index_meeting(meeting_id: str, filename: str, intelligence: MeetingIntelligence) -> None:
    """Embed this meeting's transcript, summary, decisions, action items,
    risks, and graph-relevant entities into meeting_snippets/decisions.
    Upsert, so re-processing a meeting updates in place rather than
    duplicating.

    Graph-node text is built from intelligence.knowledge_triples/
    participants directly rather than by querying Neo4j: this function runs
    inside _analyze_transcript, before _save_and_graph calls
    graph_builder.build_from_meeting, so the graph doesn't exist yet at this
    point — MeetingIntelligence already carries what's needed in memory."""
    meeting_title = filename  # every call site actually passes meeting.title, not a filename
    snippets = get_snippets_collection()

    def _base_meta(**extra) -> dict:
        return {
            "timestamp": "",
            "speaker": "",
            "source": meeting_title,
            "meeting_id": meeting_id,
            "meeting_title": meeting_title,
            **extra,
        }

    ids, documents, metadatas = [], [], []
    for idx, line in enumerate(intelligence.transcript):
        full_line = f"[{line.timestamp}] {line.speaker}: {line.text}"
        ids.append(f"{meeting_id}_transcript_{idx:04d}")
        documents.append(full_line)
        metadatas.append(_base_meta(
            timestamp=line.timestamp, speaker=line.speaker, full_text=line.text, type="transcript",
        ))
    if ids:
        snippets.upsert(ids=ids, documents=documents, metadatas=metadatas)

    if intelligence.decisions or intelligence.action_items:
        summary_lines = [f"SUMMARY FOR: {meeting_title}"]
        if intelligence.decisions:
            summary_lines.append("Decisions:")
            summary_lines += [f"- {d.text}" for d in intelligence.decisions]
        if intelligence.action_items:
            summary_lines.append("Action Items:")
            summary_lines += [f"- {a.task} (Assignee: {a.assignee})" for a in intelligence.action_items]
        summary_text = "\n".join(summary_lines)
        snippets.upsert(
            ids=[f"{meeting_id}_summary"],
            documents=[summary_text],
            metadatas=[_base_meta(
                timestamp="00:00:00", speaker="System", full_text=summary_text, type="summary",
            )],
        )

    if intelligence.action_items:
        ids, documents, metadatas = [], [], []
        for idx, item in enumerate(intelligence.action_items):
            ids.append(f"{meeting_id}_action_{idx:04d}")
            documents.append(item.task)
            metadatas.append(_base_meta(
                speaker=item.assignee or "", full_text=item.task, type="action_item",
                assignee=item.assignee or "", deadline=item.deadline or "",
            ))
        snippets.upsert(ids=ids, documents=documents, metadatas=metadatas)

    if intelligence.risks:
        ids, documents, metadatas = [], [], []
        for idx, risk in enumerate(intelligence.risks):
            ids.append(f"{meeting_id}_risk_{idx:04d}")
            documents.append(risk)
            metadatas.append(_base_meta(full_text=risk, type="risk"))
        snippets.upsert(ids=ids, documents=documents, metadatas=metadatas)

    graph_node_sentences = [f"{name} participated in {meeting_title}." for name in intelligence.participants]
    for triple in intelligence.knowledge_triples:
        predicate_text = triple.predicate.replace("_", " ").lower()
        graph_node_sentences.append(f"{triple.subject} {predicate_text} {triple.object} (from {meeting_title}).")
    if graph_node_sentences:
        ids, documents, metadatas = [], [], []
        for idx, sentence in enumerate(graph_node_sentences):
            ids.append(f"{meeting_id}_graphnode_{idx:04d}")
            documents.append(sentence)
            metadatas.append(_base_meta(full_text=sentence, type="graph_node"))
        snippets.upsert(ids=ids, documents=documents, metadatas=metadatas)

    decisions = get_decisions_collection()
    for decision in intelligence.decisions:
        decision_id = f"{meeting_id}_{_stable_hash(decision.text)}"
        decisions.upsert(
            ids=[decision_id],
            documents=[decision.text],
            metadatas=[{
                "meeting_id": meeting_id,
                "meeting_title": meeting_title,
                "text": decision.text,
                "timestamp": decision.timestamp,
                "speaker": decision.speaker or "",
                "type": "decision",
            }],
        )


def query_snippets(question: str, n_results: int = 5, meeting_ids: set[str] | None = None) -> dict:
    """(Task 5.5) Semantic search over transcript snippets + summaries for
    Ask Coco's RAG context. `meeting_ids=None` (the default) is
    unrestricted; an empty set means the caller has zero accessible
    meetings, short-circuited here rather than sent to Chroma as an empty
    `$in` list."""
    if meeting_ids is not None and not meeting_ids:
        return {"documents": [[]], "metadatas": [[]]}
    collection = get_snippets_collection()
    if collection.count() == 0:
        return {"documents": [[]], "metadatas": [[]]}
    kwargs: dict = {"query_texts": [question], "n_results": min(n_results, collection.count())}
    if meeting_ids is not None:
        kwargs["where"] = {"meeting_id": {"$in": list(meeting_ids)}}
    return collection.query(**kwargs)


def delete_meeting(meeting_id: str) -> None:
    """Remove this meeting's entries from both collections (transcript/summary
    snippets and decision embeddings), keyed on the meeting_id every entry is
    upserted with in index_meeting() above."""
    get_snippets_collection().delete(where={"meeting_id": meeting_id})
    get_decisions_collection().delete(where={"meeting_id": meeting_id})


def query_similar_decisions(
    decision_text: str, exclude_meeting_id: str, n_results: int = 3, meeting_ids: set[str] | None = None
) -> list[dict]:
    """(Task 4.4) Nearest-neighbor past decisions from OTHER meetings.
    `meeting_ids=None` (the default) is unrestricted — required for
    contradiction_service's system-initiated, caller-less comparisons
    against every past decision. Only askcoco_service passes a real,
    caller-scoped set; an empty set short-circuits to no results."""
    if meeting_ids is not None and not meeting_ids:
        return []
    collection = get_decisions_collection()
    if collection.count() == 0:
        return []

    kwargs: dict = {"query_texts": [decision_text], "n_results": min(n_results + 3, collection.count())}
    if meeting_ids is not None:
        kwargs["where"] = {"meeting_id": {"$in": list(meeting_ids)}}
    results = collection.query(**kwargs)

    matches = []
    if results["documents"] and results["documents"][0]:
        for doc, meta, dist in zip(
            results["documents"][0], results["metadatas"][0], results["distances"][0]
        ):
            if meta.get("meeting_id") == exclude_meeting_id:
                continue
            matches.append({"text": doc, "meeting_id": meta.get("meeting_id"), "distance": dist})
            if len(matches) >= n_results:
                break
    return matches
