from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core.config import get_settings
from app.core.exceptions import unhandled_exception_handler
from app.core.logger import get_logger
from app.core.middleware import log_requests
from app.database.session import Base, SessionLocal, engine, sync_missing_columns
from app.graph import graph_builder
from app.models import meeting as _meeting_models  # noqa: F401 - registers models on Base
from app.models import employee as _employee_models  # noqa: F401 - registers models on Base
from app.models.employee import Employee

settings = get_settings()
logger = get_logger(__name__)

Base.metadata.create_all(bind=engine)
sync_missing_columns()

# Demo employee directory — mirrors mockData.ts's INITIAL_EMPLOYEES_DATA,
# the frontend's actual login/demo-switcher roster. This is the backend's
# only source of role (is_management); request identity is asserted via the
# X-User-Name header (see app/core/auth.py) and looked up here, not trusted
# directly. Keep this in sync by hand when the frontend roster changes —
# there's no shared source of truth between the two yet.
_DEMO_EMPLOYEES = [
    ("Thim Yee Song", "thim.yeesong@corpbrain.ai", "VP of Product", True),
    ("Duncan", "duncan@corpbrain.ai", "VP of Engineering", True),
    ("Kam Xin Le", "kam.xinle@corpbrain.ai", "Head of Product", True),
    ("Yap En Yu", "yap.enyu@corpbrain.ai", "Chief Financial Officer", True),
]


def _seed_employees() -> None:
    """Upserts by name rather than a one-time "table is empty" check — the
    empty-check version silently stopped applying roster changes the moment
    the table was first seeded, which is exactly how this list went stale
    against mockData.ts after a merge updated the frontend roster but an
    already-seeded dev DB never re-ran this function."""
    db = SessionLocal()
    try:
        for name, email, title, is_management in _DEMO_EMPLOYEES:
            if db.query(Employee).filter(Employee.name == name).first() is None:
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
    _allowed_origins.extend(
        origin.strip().rstrip("/")
        for origin in settings.frontend_origin.split(",")
        if origin.strip()
    )

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
