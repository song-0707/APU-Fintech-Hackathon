from app.services import brief_service


def test_brief_contradictions_scope_both_sides(monkeypatch):
    captured = {}

    def fake_run_query(cypher, **params):
        captured["cypher"] = cypher
        captured["params"] = params
        return []

    monkeypatch.setattr(brief_service, "run_query", fake_run_query)

    result = brief_service._contradictions_for(["m1"])

    assert result == []
    assert "otherMeeting.id IN $ids" in captured["cypher"]
    assert captured["params"]["ids"] == ["m1"]
