from app.services import gemini_client


class _FakeModels:
    def __init__(self, text: str | None = None, exc: Exception | None = None) -> None:
        self.text = text
        self.exc = exc
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return type("Response", (), {"text": self.text})()


class _FakeClient:
    def __init__(self, text: str | None = None, exc: Exception | None = None) -> None:
        self.models = _FakeModels(text=text, exc=exc)


def test_generate_content_falls_back_from_service_account_to_api_key(monkeypatch):
    service_client = _FakeClient(exc=PermissionError("missing aiplatform permission"))
    api_client = _FakeClient(text="ok")
    monkeypatch.setattr(
        gemini_client,
        "get_gemini_clients",
        lambda: [
            ("service-account", service_client),
            ("api-key", api_client),
        ],
    )

    response = gemini_client.generate_content(model="gemini-3.6-flash", contents="ping")

    assert response.text == "ok"
    assert service_client.models.calls[0]["model"] == "gemini-2.5-flash"
    assert api_client.models.calls[0]["model"] == "gemini-3.6-flash"


def test_generate_content_raises_when_no_credentials(monkeypatch):
    monkeypatch.setattr(gemini_client, "get_gemini_clients", lambda: [])

    try:
        gemini_client.generate_content(model="gemini-3.6-flash", contents="ping")
    except ValueError as exc:
        assert "No Gemini credentials configured" in str(exc)
    else:
        raise AssertionError("Expected generate_content to fail without credentials")
