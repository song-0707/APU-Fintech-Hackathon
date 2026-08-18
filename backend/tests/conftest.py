import os

os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("NEO4J_PASSWORD", "test-password")
os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("DEEPGRAM_API_KEY", "")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.session import Base, get_db
from app.main import app
from app.models.employee import Employee


@pytest.fixture
def db_session():
    """Isolated in-memory SQLite DB for one test, overriding get_db() so
    every route dependency (including get_current_employee) sees it instead
    of the real configured database. StaticPool pins every connection from
    this engine to the same underlying sqlite3 connection — without it,
    each new Session opens a *separate* (and separately empty) ":memory:"
    database."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def management_employee(db_session):
    """One seeded, management-flagged Employee — the default caller
    identity for tests that don't care about access-control specifics, just
    that *a* recognized, unrestricted caller is present."""
    employee = Employee(name="Test Manager", email="test.manager@corpbrain.ai", is_management=True)
    db_session.add(employee)
    db_session.commit()
    return employee


@pytest.fixture
def caller_headers(management_employee):
    return {"X-User-Name": management_employee.name}
