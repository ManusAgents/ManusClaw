import os
import asyncio
import tempfile
import pytest

# Use mock LLM for all tests
os.environ["APP_ENV"] = "test"
_tmp_ws = tempfile.mkdtemp()
os.environ.setdefault("MANUSCLAW_WORKSPACE", _tmp_ws)


@pytest.fixture
def tmp_workspace(tmp_path):
    prev = os.environ.get("MANUSCLAW_WORKSPACE", "")
    os.environ["MANUSCLAW_WORKSPACE"] = str(tmp_path)
    yield tmp_path
    if prev:
        os.environ["MANUSCLAW_WORKSPACE"] = prev
    else:
        os.environ.pop("MANUSCLAW_WORKSPACE", None)


@pytest.fixture
def tmp_db(tmp_path):
    from app.db.session import SessionDB
    db = SessionDB(db_path=tmp_path / "test.db")
    yield db
    db.close()
