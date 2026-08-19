"""Access-control tests for /query, /api/chat, and askcoco_service.ask —
the boundary the original audit flagged as untested. askcoco_service.py
does `from app.graph.neo4j_service import run_query`, binding its own
local name, so tests must patch `askcoco_service.run_query` directly
(patching neo4j_service.run_query would not affect calls made from inside
askcoco_service)."""
from app.models.employee import Employee, MeetingParticipant
from app.services import askcoco_service, embedding_service


def test_query_requires_identity_header(client, db_session):
    response = client.post("/query", json={"query": "what decisions were made?"})
    assert response.status_code == 401


def test_employee_search_is_scoped_to_their_meetings(client, db_session, monkeypatch):
    employee = Employee(name="Alice Chen", email="alice.chen@corpbrain.ai", is_management=False)
    db_session.add(employee)
    db_session.commit()
    db_session.add(MeetingParticipant(meeting_id="m1", employee_id=employee.id))
    db_session.commit()

    captured = {}

    def fake_query_snippets(question, n_results=5, meeting_ids=None):
        captured["snippets_meeting_ids"] = meeting_ids
        return {"documents": [[]], "metadatas": [[]], "distances": [[]]}

    def fake_query_similar_decisions(decision_text, exclude_meeting_id, n_results=3, meeting_ids=None):
        captured["decisions_meeting_ids"] = meeting_ids
        return []

    monkeypatch.setattr(embedding_service, "query_snippets", fake_query_snippets)
    monkeypatch.setattr(embedding_service, "query_similar_decisions", fake_query_similar_decisions)
    # "tell me about the roadmap" matches none of the 4 keyword templates,
    # so after the (mocked, empty) semantic search it falls through to the
    # generic _meetings template — mock that too so it doesn't hit a real
    # Neo4j and return the unrelated "graph unavailable" message instead.
    monkeypatch.setattr(askcoco_service, "run_query", lambda *a, **k: [])

    response = client.post(
        "/query", json={"query": "tell me about the roadmap"}, headers={"X-User-Name": "Alice Chen"}
    )

    assert response.status_code == 200
    assert captured["snippets_meeting_ids"] == {"m1"}
    assert captured["decisions_meeting_ids"] == {"m1"}
    # No hits at all -> the fixed refusal, not a fabricated answer.
    assert response.json()["answer"] == askcoco_service._NO_CONTEXT_ANSWER
    assert response.json()["citations"] == []


def test_management_search_is_unrestricted(client, db_session, caller_headers, monkeypatch):
    captured = {}

    def fake_query_snippets(question, n_results=5, meeting_ids=None):
        captured["meeting_ids"] = meeting_ids
        return {"documents": [[]], "metadatas": [[]], "distances": [[]]}

    monkeypatch.setattr(embedding_service, "query_snippets", fake_query_snippets)
    monkeypatch.setattr(embedding_service, "query_similar_decisions", lambda *a, **k: [])

    response = client.post("/query", json={"query": "tell me anything"}, headers=caller_headers)

    assert response.status_code == 200
    assert captured["meeting_ids"] is None


def test_employee_with_no_accessible_meetings_gets_refusal_without_querying(client, db_session, monkeypatch):
    employee = Employee(name="Bob Diaz", email="bob.diaz@corpbrain.ai", is_management=False)
    db_session.add(employee)
    db_session.commit()

    called = {"snippets": False, "decisions": False}
    monkeypatch.setattr(embedding_service, "query_snippets", lambda *a, **k: called.__setitem__("snippets", True))
    monkeypatch.setattr(embedding_service, "query_similar_decisions", lambda *a, **k: called.__setitem__("decisions", True))

    response = client.post("/query", json={"query": "what happened?"}, headers={"X-User-Name": "Bob Diaz"})

    assert response.status_code == 200
    assert response.json()["answer"] == askcoco_service._NO_CONTEXT_ANSWER
    assert called == {"snippets": False, "decisions": False}


def test_decisions_template_is_scoped_by_meeting_ids(monkeypatch):
    captured = {}

    def fake_run_query(cypher, **params):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"decision": "Ship it", "confidence": "firm_commitment", "speaker": "Alice", "reason": None, "evidence": None, "meeting": "Vendor Review"}]

    monkeypatch.setattr(askcoco_service, "run_query", fake_run_query)

    result = askcoco_service.ask("what decisions were made?", meeting_ids={"m1"})

    assert "meeting_ids" in captured["params"]
    assert captured["params"]["meeting_ids"] == ["m1"]
    assert "$meeting_ids" in captured["cypher"]
    assert result["results"][0]["decision"] == "Ship it"


def test_contradiction_query_scopes_both_sides(monkeypatch):
    """The Cypher itself must require both the current and previous
    decision's meetings to be in scope -- this test asserts the query text
    does that, since the actual filtering happens inside Neo4j (mocked
    here), not in Python."""
    captured = {}

    def fake_run_query(cypher, **params):
        captured["cypher"] = cypher
        captured["params"] = params
        return []

    monkeypatch.setattr(askcoco_service, "run_query", fake_run_query)

    askcoco_service.ask("any contradictions or conflicts?", meeting_ids={"m1"})

    cypher = captured["cypher"]
    assert "previousMeeting.id IN $meeting_ids" in cypher
    assert "currentMeeting.id IN $meeting_ids" in cypher


def test_find_meeting_does_not_match_outside_accessible_set(monkeypatch):
    def fake_run_query(cypher, **params):
        # Simulate Neo4j actually applying the WHERE — the fixture only
        # returns what a real scoped query would.
        assert params.get("meeting_ids") == ["m1"]
        return [{"id": "m1", "title": "Roadmap Sync"}]

    monkeypatch.setattr(askcoco_service, "run_query", fake_run_query)

    found = askcoco_service._find_meeting("summarize the Roadmap Sync meeting", meeting_ids={"m1"})
    assert found["id"] == "m1"


def test_citations_reference_the_retrieved_meeting(monkeypatch):
    def fake_run_query(cypher, **params):
        return [{
            "decision": "Adopt Provider X", "confidence": "firm_commitment",
            "speaker": "Duncan", "reason": "cost", "evidence": "22% cheaper",
            "meeting": "Vendor Review",
        }]

    monkeypatch.setattr(askcoco_service, "run_query", fake_run_query)
    monkeypatch.setattr(askcoco_service, "_synthesize_with_gemini", lambda *a, **k: None)

    result = askcoco_service.ask("what decisions were made?", meeting_ids=None)

    assert result["citations"][0]["filename"] == "Vendor Review"
    assert result["citations"][0]["speaker"] == "Duncan"
    assert result["citations"][0]["excerpt"] == "Adopt Provider X"


def test_semantic_retrieval_failure_falls_back_to_scoped_meetings_query(monkeypatch):
    captured = {}

    def fake_run_query(cypher, **params):
        captured["cypher"] = cypher
        captured["params"] = params
        return []

    monkeypatch.setattr(embedding_service, "query_similar_decisions", lambda *a, **k: (_ for _ in ()).throw(ValueError("embedding unavailable")))
    monkeypatch.setattr(embedding_service, "query_snippets", lambda *a, **k: (_ for _ in ()).throw(ValueError("embedding unavailable")))
    monkeypatch.setattr(askcoco_service, "run_query", fake_run_query)

    result = askcoco_service.ask("tell me about the roadmap", meeting_ids={"m1"})

    assert result["answer"] == askcoco_service._NO_CONTEXT_ANSWER
    assert result["results"] == []
    assert "MATCH (m:Meeting)" in captured["cypher"]
    assert captured["params"]["meeting_ids"] == ["m1"]


def test_empty_template_results_refuse_without_calling_gemini(monkeypatch):
    called = {"gemini": False}

    monkeypatch.setattr(askcoco_service, "run_query", lambda *a, **k: [])

    def fake_synthesize(*args, **kwargs):
        called["gemini"] = True
        return "invented answer"

    monkeypatch.setattr(askcoco_service, "_synthesize_with_gemini", fake_synthesize)

    result = askcoco_service.ask("what decisions were made?", meeting_ids={"m1"})

    assert result["answer"] == askcoco_service._NO_CONTEXT_ANSWER
    assert result["results"] == []
    assert result["citations"] == []
    assert called["gemini"] is False


def test_bare_greeting_gets_a_canned_reply_without_any_retrieval(monkeypatch):
    """Regression test: "hi" used to fall through every _TEMPLATES keyword,
    then _semantic_expand's nearest-neighbor search would score it "close
    enough" to real decisions on this small demo corpus and return them —
    a random meeting-data dump in response to a greeting."""
    called = {"query": False, "snippets": False, "decisions": False}

    monkeypatch.setattr(askcoco_service, "run_query", lambda *a, **k: called.__setitem__("query", True) or [])
    monkeypatch.setattr(embedding_service, "query_snippets", lambda *a, **k: called.__setitem__("snippets", True) or {"documents": [[]], "metadatas": [[]], "distances": [[]]})
    monkeypatch.setattr(embedding_service, "query_similar_decisions", lambda *a, **k: called.__setitem__("decisions", True) or [])

    for greeting in ["hi", "Hi!", "  hello  ", "THANKS", "what's up"]:
        result = askcoco_service.ask(greeting, meeting_ids={"m1"})
        assert result["answer"] == askcoco_service._GREETING_ANSWER
        assert result["results"] == []
        assert result["citations"] == []

    assert called == {"query": False, "snippets": False, "decisions": False}


def test_greeting_word_inside_a_real_question_still_retrieves(monkeypatch):
    """Exact-match, not substring — "hi" as a standalone message is
    smalltalk, but a real question that happens to start with a greeting
    word must still reach the normal template/semantic path."""
    result = askcoco_service.ask("hi, what decisions were made?", meeting_ids={"m1"})
    assert result["answer"] != askcoco_service._GREETING_ANSWER
