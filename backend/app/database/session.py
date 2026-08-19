from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import get_settings
from app.core.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)

database_url = settings.database_url
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

_connect_args = {}
if database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}
elif database_url.startswith("postgresql") and "sslmode=" not in database_url:
    _connect_args = {"sslmode": "require"}

engine = create_engine(database_url, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception as exc:
        logger.error(f"Unhandled DB session error: {exc}", exc_info=True)
        raise
    finally:
        db.close()
