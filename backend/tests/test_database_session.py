from sqlalchemy import create_engine, text

from app.database import session as db_session


def test_sync_missing_columns_adds_missing_nullable_column_and_is_idempotent(tmp_path, monkeypatch):
    """Regression test for the schema-drift bug that broke the real dev DB:
    create_all() never alters a table that already exists, so a pre-existing
    file that predates a new nullable model column keeps failing inserts
    forever unless something patches it. Simulates that exact situation
    against a throwaway file (not the real corporate_brain.db)."""
    db_path = tmp_path / "drift.db"
    temp_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    with temp_engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE meetings (id VARCHAR PRIMARY KEY, title VARCHAR NOT NULL, "
            "project VARCHAR, date VARCHAR, duration VARCHAR, file_path VARCHAR, "
            "status VARCHAR NOT NULL, progress INTEGER NOT NULL, "
            "created_at DATETIME, updated_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO meetings (id, title, status, progress) VALUES ('m1', 'Old Row', 'pending', 0)"
        ))

    monkeypatch.setattr(db_session, "engine", temp_engine)
    db_session.sync_missing_columns()
    db_session.sync_missing_columns()  # must be safe to run again on every startup

    with temp_engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(meetings)"))}
        assert "source" in columns
        assert "room_id" in columns

        row = conn.execute(
            text("SELECT title, source, room_id FROM meetings WHERE id = 'm1'")
        ).fetchone()
        assert row[0] == "Old Row"
        assert row[1] is None
        assert row[2] is None


def test_sync_missing_columns_skips_new_tables(tmp_path, monkeypatch):
    """A table that doesn't exist yet is create_all()'s job, not this
    function's — sync_missing_columns must not try to ALTER a table that
    isn't there."""
    db_path = tmp_path / "empty.db"
    temp_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    monkeypatch.setattr(db_session, "engine", temp_engine)
    db_session.sync_missing_columns()  # no tables exist at all — must be a no-op, not an error
