from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core.config import get_settings
from app.core.exceptions import unhandled_exception_handler
from app.core.logger import get_logger
from app.core.middleware import log_requests
from app.database.session import Base, SessionLocal, engine
from app.graph import graph_builder
from app.models import meeting as _meeting_models  # noqa: F401 - registers models on Base
from app.models import employee as _employee_models  # noqa: F401 - registers models on Base
from app.models.employee import Employee

settings = get_settings()
logger = get_logger(__name__)

Base.metadata.create_all(bind=engine)

# Demo employee directory — reuses AppContext.tsx's initialEmployees
# names/titles since those are the ones that already appear as speakers in
# the bundled demo meeting data. This is the backend's only source of role
# (is_management); request identity is asserted via the X-User-Name header
# (see app/core/auth.py) and looked up here, not trusted directly.
_DEMO_EMPLOYEES = [
    ("Alex Mercer", "alex.mercer@corpbrain.ai", "VP of Product", True),
    ("Sarah Jenkins", "sarah.jenkins@corpbrain.ai", "VP of Engineering", True),
    ("Marcus Vance", "marcus.vance@corpbrain.ai", "Head of Product", True),
    ("Elena Rostova", "elena.rostova@corpbrain.ai", "Chief Financial Officer", True),
    ("David Chen", "david.chen@corpbrain.ai", "Principal AI Architect", False),
    ("Amanda Brooks", "amanda.brooks@corpbrain.ai", "General Counsel", True),
]


def _seed_employees() -> None:
    db = SessionLocal()
    try:
        if db.query(Employee).first() is None:
            for name, email, title, is_management in _DEMO_EMPLOYEES:
                db.add(Employee(name=name, email=email, title=title, is_management=is_management))
            db.commit()
    finally:
        db.close()


_seed_employees()

try:
    graph_builder.ensure_constraints()
except Exception as e:
    logger.warning(f"Could not set up Neo4j constraints at startup (is Neo4j running?): {e}")

app = FastAPI(title="Corporate Brain API")

_allowed_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    # Vite may bind to IPv6 loopback and browsers then use this origin.
    "http://[::1]:5173",
]
if settings.frontend_origin:
    _allowed_origins.append(settings.frontend_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    # Development clients may open Vite through the host computer's private
    # LAN address. Public deployments should use explicit HTTPS origins.
    allow_origin_regex=(
        r"^https?://(?:localhost|127\.0\.0\.1|\[::1\]|10(?:\.\d{1,3}){3}|"
        r"192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])"
        r"(?:\.\d{1,3}){2})(?::\d+)?$"
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(log_requests)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(api_router)

logger.info("Corporate Brain API starting")
