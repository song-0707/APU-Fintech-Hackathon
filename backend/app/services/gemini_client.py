from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.logger import get_logger

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
logger = get_logger(__name__)


def has_gemini_credentials(settings: Settings | None = None) -> bool:
    """Callers that hold their own `settings` object (e.g. one a test has
    monkeypatched) should pass it explicitly — otherwise this falls back to
    the process-wide cached settings singleton, which a per-module patch
    can't reach."""
    settings = settings or get_settings()
    return bool(
        settings.gemini_api_key
        or settings.gemini_service_account_file
        or settings.google_application_credentials
    )


def _service_account_client():
    settings = get_settings()
    credential_file = settings.gemini_service_account_file or settings.google_application_credentials
    if not credential_file:
        return None

    from google import genai
    from google.oauth2 import service_account

    path = Path(credential_file).expanduser()
    credentials = service_account.Credentials.from_service_account_file(
        str(path),
        scopes=[_CLOUD_PLATFORM_SCOPE],
    )
    project = settings.gemini_project_id or credentials.project_id
    return genai.Client(
        vertexai=True,
        credentials=credentials,
        project=project,
        location=settings.gemini_location,
    )


def _api_key_client():
    settings = get_settings()
    if not settings.gemini_api_key:
        return None

    from google import genai

    return genai.Client(api_key=settings.gemini_api_key)


def get_gemini_clients() -> list[tuple[str, object]]:
    clients: list[tuple[str, object]] = []
    try:
        service_account = _service_account_client()
        if service_account is not None:
            clients.append(("service-account", service_account))
    except Exception as exc:
        logger.warning("Gemini service-account client could not be loaded: %s", exc)
    api_key = _api_key_client()
    if api_key is not None:
        clients.append(("api-key", api_key))
    return clients


def get_gemini_client():
    """Return the preferred Google GenAI client, or None if unconfigured."""
    clients = get_gemini_clients()
    if clients:
        return clients[0][1]
    return None


def generate_content(*, model: str, contents, config=None):
    """Generate content, preferring Vertex service-account auth when present.

    If both service-account credentials and an API key are configured, a
    service-account IAM/Vertex failure falls back to the API key so local demo
    flows do not silently lose Gemini while cloud permissions are being fixed.
    """
    clients = get_gemini_clients()
    if not clients:
        raise ValueError("No Gemini credentials configured")

    last_exc: Exception | None = None
    settings = get_settings()
    for label, client in clients:
        try:
            request_model = settings.gemini_vertex_model if label == "service-account" else model
            kwargs = {"model": request_model, "contents": contents}
            if config is not None:
                kwargs["config"] = config
            return client.models.generate_content(**kwargs)
        except Exception as exc:
            last_exc = exc
            if label != clients[-1][0]:
                logger.warning("Gemini %s request failed; trying next auth mode: %s", label, exc)
                continue
            raise

    if last_exc:
        raise last_exc
    raise ValueError("No Gemini credentials configured")
