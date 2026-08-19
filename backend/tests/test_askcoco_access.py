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


def test_action_keyword_does_not_match_inside_unrelated_words(monkeypatch):
    """Regression: "satisfaction" contains the action-item keyword "action"
    as a raw substring, so the old `keyword in lowered_query` check
    misrouted any question about "satisfaction" to the action-items
    template instead of decisions."""
    captured = {}

    def fake_run_query(cypher, **params):
        captured["cypher"] = cypher
        return [{
            "decision": "Refresh satisfaction score every 6 hours",
            "confidence": "soft_agreement", "speaker": "Kam Xin Le",
            "reason": None, "evidence": None, "timestamp": "00:03:22",
            "meeting": "customer feedback dashboard",
        }]

    monkeypatch.setattr(askcoco_service, "run_query", fake_run_query)
    monkeypatch.setattr(askcoco_service, "_synthesize_with_gemini", lambda *a, **k: None)

    askcoco_service.ask(
        "What decisions were made about the satisfaction score refresh rate?",
        meeting_ids={"m1"},
    )

    assert "MATCH (d:Decision)" in captured["cypher"]
    assert "ActionItem" not in captured["cypher"]


def test_keyword_still_matches_its_own_plural(monkeypatch):
    """The boundary fix must not regress the substring-reliant plurals the
    keyword list already depends on (e.g. "decision" -> "decisions")."""
    monkeypatch.setattr(askcoco_service, "run_query", lambda *a, **k: [])

    kind_used = askcoco_service._select_template("what decisions were made?")[1]

    assert kind_used == "decisions"


def test_citations_are_filtered_to_the_relevant_row(monkeypatch):
    """Regression: the decisions/action-items/contradictions templates
    fetch every row of that type across every accessible meeting with no
    topical filter, so a question about one specific decision used to cite
    every unrelated decision too. Once at least one row shares real content
    words with the query, unrelated rows must be dropped."""
    rows = [
        {"decision": "Adopt Provider X for hosting", "confidence": "firm_commitment",
         "speaker": "Duncan", "reason": "cost", "evidence": None, "timestamp": "00:01:00",
         "meeting": "Vendor Contract Review"},
        {"decision": "Top five complaints on main page, rest in a separate tab",
         "confidence": "firm_commitment", "speaker": "Thim Yee Song", "reason": None,
         "evidence": None, "timestamp": "00:02:56", "meeting": "customer feedback dashboard"},
    ]
    monkeypatch.setattr(askcoco_service, "run_query", lambda *a, **k: rows)
    monkeypatch.setattr(askcoco_service, "_synthesize_with_gemini", lambda *a, **k: None)

    result = askcoco_service.ask(
        "What did the team decide about which complaint categories to show on the main page?",
        meeting_ids=None,
    )

    assert len(result["citations"]) == 1
    assert result["citations"][0]["filename"] == "customer feedback dashboard"


def test_citations_fall_back_to_full_set_when_nothing_scores(monkeypatch):
    """A query with no scorable content words (or one that matches nothing)
    must not zero out citations for a real retrieval -- fall back to the
    original set rather than showing none."""
    rows = [{"decision": "Ship it", "confidence": "firm_commitment", "speaker": "Alice",
             "reason": None, "evidence": None, "timestamp": "00:00:05", "meeting": "Vendor Review"}]
    monkeypatch.setattr(askcoco_service, "run_query", lambda *a, **k: rows)
    monkeypatch.setattr(askcoco_service, "_synthesize_with_gemini", lambda *a, **k: None)

    result = askcoco_service.ask("what decisions were made?", meeting_ids=None)

    assert len(result["citations"]) == 1


def test_decision_citations_include_the_real_timestamp(monkeypatch):
    monkeypatch.setattr(askcoco_service, "run_query", lambda *a, **k: [
        {"decision": "Ship it", "confidence": "firm_commitment", "speaker": "Alice",
         "reason": None, "evidence": None, "timestamp": "00:03:22", "meeting": "Vendor Review"},
    ])
    monkeypatch.setattr(askcoco_service, "_synthesize_with_gemini", lambda *a, **k: None)

    result = askcoco_service.ask("what decisions were made?", meeting_ids=None)

    assert result["citations"][0]["timestamp"] == "00:03:22"


def test_semantic_search_surfaces_matched_transcript_snippets(monkeypatch):
    """Regression: a fact mentioned only in passing (never promoted to a
    Decision/ActionItem/Contradiction node) is findable by the transcript
    snippet vector search, but _semantic_expand used to read only the
    meeting_id off the snippet hit and then discard the matched line,
    substituting that meeting's formal decisions instead -- so the fact
    stayed unanswerable even though retrieval found it. The matched
    snippet itself must now survive into results/citations."""
    monkeypatch.setattr(embedding_service, "query_similar_decisions", lambda *a, **k: [])
    monkeypatch.setattr(
        embedding_service,
        "query_snippets",
        lambda *a, **k: {
            "documents": [["[00:01:02] Alex Chen: Maybe half a day to fix the category mapping, depending on other duplicate labels."]],
            "metadatas": [[{
                "meeting_id": "m1", "meeting_title": "customer feedback dashboard",
                "type": "transcript", "speaker": "Alex Chen", "timestamp": "00:01:02",
                "full_text": "Maybe half a day to fix the category mapping, depending on other duplicate labels.",
            }]],
            "distances": [[0.5]],
        },
    )
    monkeypatch.setattr(askcoco_service, "run_query", lambda *a, **k: [])
    monkeypatch.setattr(askcoco_service, "_synthesize_with_gemini", lambda *a, **k: None)

    result = askcoco_service.ask("How long will the category mapping fix take?", meeting_ids={"m1"})

    assert any("half a day" in c["excerpt"] for c in result["citations"])
    assert any(c["speaker"] == "Alex Chen" and c["timestamp"] == "00:01:02" for c in result["citations"])


def test_semantic_search_surfaces_legacy_untyped_transcript_snippets(monkeypatch):
    """Regression: meetings indexed before the `type` metadata field
    existed on snippet entries have no `type` key at all -- not
    `type: "transcript"`, just missing. A strict `meta.get("type") ==
    "transcript"` check silently drops every one of these older meetings'
    snippets, including genuine transcript lines, while summary blobs
    (identifiable by their "SUMMARY FOR:" prefix even without a type tag)
    must still be excluded."""
    monkeypatch.setattr(embedding_service, "query_similar_decisions", lambda *a, **k: [])
    monkeypatch.setattr(
        embedding_service,
        "query_snippets",
        lambda *a, **k: {
            "documents": [[
                "[00:03:40] Thim Yee Song: I'll finish the category mapping by Thursday.",
                "SUMMARY FOR: customer feedback dashboard",
            ]],
            "metadatas": [[
                {
                    "source": "customer feedback dashboard", "meeting_id": "m1",
                    "speaker": "Thim Yee Song", "timestamp": "00:03:40",
                    "full_text": "I'll finish the category mapping by Thursday.",
                },
                {
                    "source": "customer feedback dashboard", "meeting_id": "m1",
                    "speaker": "System", "timestamp": "00:00:00",
                    "full_text": "SUMMARY FOR: customer feedback dashboard\n...",
                },
            ]],
            "distances": [[0.55, 0.6]],
        },
    )
    monkeypatch.setattr(askcoco_service, "run_query", lambda *a, **k: [])
    monkeypatch.setattr(askcoco_service, "_synthesize_with_gemini", lambda *a, **k: None)

    result = askcoco_service.ask("When will the category mapping be finished?", meeting_ids={"m1"})

    excerpts = [c["excerpt"] for c in result["citations"]]
    assert any("Thursday" in e for e in excerpts)
    assert not any("SUMMARY FOR" in e for e in excerpts)
    assert result["citations"][0]["filename"] == "customer feedback dashboard"


def test_action_items_cypher_guards_against_the_optional_match_where_gotcha():
    """A WHERE clause written directly after OPTIONAL MATCH
    (a)-[:MADE_IN]->(m:Meeting) is folded into that match's own predicate
    in Cypher, not applied as a row filter -- confirmed against a real
    Neo4j read during development: without a WITH a, p, m in between, an
    action item whose real meeting fails the $meeting_ids check doesn't
    get excluded, it comes back with m silently nulled out, leaking its
    task/assignee text past the access-control scope this file's
    docstring promises. Mocked run_query tests (every other test in this
    file) can't catch that class of bug at all, since they replace the
    very component -- Neo4j's query semantics -- it lives in. This is a
    structural tripwire: assert the guard is still there rather than
    re-deriving the live-database proof on every run."""
    cypher, _ = askcoco_service._action_items("action items?", {"m1"})
    assert "OPTIONAL MATCH (a)-[:MADE_IN]->(m:Meeting) " in cypher
    with_idx = cypher.index("OPTIONAL MATCH (a)-[:MADE_IN]->(m:Meeting) ") + len(
        "OPTIONAL MATCH (a)-[:MADE_IN]->(m:Meeting) "
    )
    where_idx = cypher.index("WHERE")
    between = cypher[with_idx:where_idx]
    assert "WITH" in between and "m" in between, (
        "the OPTIONAL MATCH must be followed by a WITH before WHERE, or "
        "$meeting_ids silently stops filtering action items by meeting"
    )


def test_named_meeting_in_query_scopes_action_items_to_that_meeting_only(monkeypatch):
    """Regression: asking for action items "in this customer feedback
    dashboard" meeting used to run unscoped across every accessible
    meeting and rely on token-overlap ranking of each task's own text to
    sort out relevance -- which fails whenever an unrelated meeting's task
    happens to reuse words from the named meeting's title (e.g. "Present
    customer feedback project to CEO" from a different meeting outscoring
    genuine in-meeting tasks that don't repeat "customer feedback
    dashboard" in their own text). Naming a real, accessible meeting must
    now scope the Cypher itself to that meeting."""
    def fake_run_query(cypher, **params):
        if "m.id AS id, m.title AS title" in cypher:
            return [
                {"id": "m1", "title": "customer feedback dashboard"},
                {"id": "m2", "title": "Live: csfbp"},
            ]
        assert params.get("meeting_ids") == ["m1"], "action-items query must be narrowed to the named meeting"
        return [{
            "task": "Draft one-page user guide", "assignee": "Yap En Yu",
            "deadline": "2026-08-18", "priority": "medium",
            "meeting": "customer feedback dashboard",
        }]

    monkeypatch.setattr(askcoco_service, "run_query", fake_run_query)
    monkeypatch.setattr(askcoco_service, "_synthesize_with_gemini", lambda *a, **k: None)

    result = askcoco_service.ask("action item in this customer feedback dashboard?", meeting_ids={"m1", "m2"})

    assert all(c["filename"] == "customer feedback dashboard" for c in result["citations"])


def test_unnamed_meeting_query_leaves_full_access_scope_untouched(monkeypatch):
    """The narrowing above must only kick in when a real meeting title is
    actually named -- a generic question shouldn't get accidentally
    restricted to whatever _find_meeting's fuzzy overlap heuristic guesses
    at."""
    captured = {}

    def fake_run_query(cypher, **params):
        if "m.id AS id, m.title AS title" in cypher:
            return [{"id": "m1", "title": "customer feedback dashboard"}]
        captured["meeting_ids"] = params.get("meeting_ids")
        return []

    monkeypatch.setattr(askcoco_service, "run_query", fake_run_query)

    askcoco_service.ask("what decisions were made?", meeting_ids={"m1", "m2"})

    assert set(captured["meeting_ids"]) == {"m1", "m2"}


def test_named_meeting_scope_skips_relevance_ranking(monkeypatch):
    """Regression: once the Cypher is already scoped to one named meeting,
    re-ranking by token overlap with the query on top of that drops
    genuine same-meeting facts whose own text doesn't happen to repeat
    words from the query -- e.g. a broad "action items in this meeting"
    query sharing no words at all with a real task like "Confirm the
    6-hour refresh". Every row from a properly meeting-scoped Cypher call
    must survive to citations, not just the ones that score above zero."""
    def fake_run_query(cypher, **params):
        if "m.id AS id, m.title AS title" in cypher:
            return [{"id": "m1", "title": "customer feedback dashboard"}]
        assert params.get("meeting_ids") == ["m1"]
        return [
            {"task": "Draft one-page user guide", "assignee": "Yap En Yu", "deadline": "2026-08-18", "priority": "medium", "meeting": "customer feedback dashboard"},
            {"task": "Confirm the 6-hour refresh", "assignee": "Kam Xin Le", "deadline": "2026-08-15", "priority": "high", "meeting": "customer feedback dashboard"},
        ]

    monkeypatch.setattr(askcoco_service, "run_query", fake_run_query)
    monkeypatch.setattr(askcoco_service, "_synthesize_with_gemini", lambda *a, **k: None)

    result = askcoco_service.ask("action item in this customer feedback dashboard?", meeting_ids={"m1"})

    assert len(result["citations"]) == 2


def test_named_meeting_scopes_semantic_search_to_that_meeting_only(monkeypatch):
    """Regression: a query naming a real meeting but matching none of the
    four keyword templates (e.g. "what is discussed in X") falls through
    to the vector-search path, which used to search every accessible
    meeting's embeddings unscoped -- so a different meeting's tangentially
    related transcript line (sharing a phrase like "customer feedback")
    could surface as a citation alongside the named meeting's real
    content. Naming a real, accessible meeting must scope both Chroma
    queries inside _semantic_expand to that meeting alone."""
    captured = {}

    def fake_run_query(cypher, **params):
        if "m.id AS id, m.title AS title" in cypher:
            return [
                {"id": "m1", "title": "customer feedback dashboard"},
                {"id": "m2", "title": "Live: csfbp"},
            ]
        return []

    def fake_query_similar_decisions(query, exclude_meeting_id, n_results=5, meeting_ids=None):
        captured["decisions_meeting_ids"] = meeting_ids
        return []

    def fake_query_snippets(query, n_results=5, meeting_ids=None):
        captured["snippets_meeting_ids"] = meeting_ids
        return {"documents": [[]], "metadatas": [[]], "distances": [[]]}

    monkeypatch.setattr(askcoco_service, "run_query", fake_run_query)
    monkeypatch.setattr(embedding_service, "query_similar_decisions", fake_query_similar_decisions)
    monkeypatch.setattr(embedding_service, "query_snippets", fake_query_snippets)

    askcoco_service.ask("what is discussed in customer feedback dashboard", meeting_ids={"m1", "m2"})

    assert captured["decisions_meeting_ids"] == {"m1"}
    assert captured["snippets_meeting_ids"] == {"m1"}


def test_snippet_speaker_resolved_against_meeting_participants(monkeypatch):
    """Regression: a raw transcript-line speaker label ("KAM") comes
    straight from diarization metadata, never through graph_builder's
    Person-node merging, so it can surface in a snippet citation as if it
    were a different person from the fuller name ("KAM XIN LE") used
    everywhere else for the same speaker in the same meeting. Resolve it
    against that meeting's real participant list when there's exactly one
    unambiguous match."""
    monkeypatch.setattr(embedding_service, "query_similar_decisions", lambda *a, **k: [])
    monkeypatch.setattr(
        embedding_service,
        "query_snippets",
        lambda *a, **k: {
            "documents": [["[00:00:04] KAM: Next one is a supplier delivery schedule."]],
            "metadatas": [[{
                "meeting_id": "m1", "meeting_title": "roadshow delivery blueprint",
                "type": "transcript", "speaker": "KAM", "timestamp": "00:00:04",
                "full_text": "Next one is a supplier delivery schedule.",
            }]],
            "distances": [[0.5]],
        },
    )

    def fake_run_query(cypher, **params):
        if "PARTICIPATED_IN" in cypher and "collect(DISTINCT p.name)" in cypher:
            return [{"meeting_id": "m1", "participants": ["Thim Yee Song", "Kam Xin Le", "Yap En Yu"]}]
        return []

    monkeypatch.setattr(askcoco_service, "run_query", fake_run_query)
    monkeypatch.setattr(askcoco_service, "_synthesize_with_gemini", lambda *a, **k: None)

    result = askcoco_service.ask("what is discussed in roadshow delivery blueprint", meeting_ids={"m1"})

    speakers = [c["speaker"] for c in result["citations"]]
    assert "Kam Xin Le" in speakers
    assert "KAM" not in speakers


def test_resolve_speaker_leaves_ambiguous_or_absent_matches_untouched():
    """Two participants both containing the raw label, or none at all,
    must not guess -- misattributing a quote to the wrong real person is
    worse than showing the raw, unresolved label."""
    assert askcoco_service._resolve_speaker("KAM", ["Kam Xin Le", "Kam Ng"]) == "KAM"
    assert askcoco_service._resolve_speaker("KAM", ["Thim Yee Song"]) == "KAM"
    assert askcoco_service._resolve_speaker("", ["Kam Xin Le"]) == ""
    assert askcoco_service._resolve_speaker("Kam Xin Le", ["Kam Xin Le"]) == "Kam Xin Le"


def test_contradiction_citations_include_speaker_and_timestamp(monkeypatch):
    monkeypatch.setattr(askcoco_service, "run_query", lambda *a, **k: [
        {"decision": "Use Provider X", "conflicts_with": "Use Provider Y",
         "message": "flip-flop on vendor choice", "speaker": "Duncan",
         "timestamp": "00:05:10", "meeting": "Vendor Review"},
    ])
    monkeypatch.setattr(askcoco_service, "_synthesize_with_gemini", lambda *a, **k: None)

    result = askcoco_service.ask("any contradictions or conflicts?", meeting_ids=None)

    assert result["citations"][0]["speaker"] == "Duncan"
    assert result["citations"][0]["timestamp"] == "00:05:10"
