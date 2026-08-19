from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import get_settings
from app.core.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)

_connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)
engine = create_engine(settings.database_url, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def sync_missing_columns() -> None:
    """Base.metadata.create_all() only creates tables that don't exist yet —
    it never adds a column to a table that's already on disk. That's fine
    for a throwaway test database, but a persistent local/demo SQLite file
    silently keeps its old schema forever once a model gains a column,
    surfacing as an OperationalError the next time that column is written.

    Patches the common case (a new nullable column on an existing table)
    with an additive ALTER TABLE so a pre-existing database doesn't need a
    hand-run migration after every model change. Anything riskier (NOT
    NULL, renames, drops, new constraints/indexes) is intentionally left
    alone — this is a stopgap, not a migration tool; reach for something
    like Alembic before this codebase needs those.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all() above already creates this one fresh
            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                if not column.nullable:
                    logger.warning(
                        "%s.%s is new and non-nullable — needs a manual migration, not auto-patching",
                        table.name, column.name,
                    )
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))
                logger.warning(
                    "Auto-added missing column %s.%s (schema drift patch)", table.name, column.name
                )


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception as exc:
        logger.error(f"Unhandled DB session error: {exc}", exc_info=True)
        raise
    finally:
        db.close()
