import os

# Set before application imports so its startup in-memory database is safe.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_MODE"] = "mock"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from app.config import Settings, get_settings
from app.database import Base, get_db, make_engine
from app.main import app


@pytest.fixture
def client():
    test_engine = make_engine("sqlite://")
    Base.metadata.create_all(bind=test_engine)
    session_factory = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    test_settings = Settings(
        database_url="sqlite://",
        internal_api_token="test-token",
        admin_username="reviewer",
        admin_password="test-password",
        ai_mode="mock",
        smtp_host="",
        followup_delay_hours=0,
        _env_file=None,
    )

    def override_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: test_settings
    with TestClient(app) as test_client:
        test_client.app.state.test_session_factory = session_factory
        yield test_client
    app.dependency_overrides.clear()
    test_engine.dispose()


@pytest.fixture
def api_headers():
    return {"X-Internal-Token": "test-token"}


@pytest.fixture
def admin_auth():
    return ("reviewer", "test-password")


@pytest.fixture
def sample():
    return {
        "name": "Jamie Lee", "email": "jamie@example.com", "company": "Sample Studio",
        "subject": "Pricing for an AI assistant", "message": "Please send a quote for appointment automation next week.",
        "consent_to_follow_up": True,
    }
