import sys
from pathlib import Path

import pytest

# Make `backend` importable regardless of how pytest is invoked
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
async def tmp_db(monkeypatch, tmp_path):
    """Point the app's SQLite database at a per-test temp file.

    get_db() reopens automatically when DB_PATH changes; the teardown close
    keeps the shared connection from leaking across event loops.
    """
    import backend.db as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", str(tmp_path / "test.db"))
    yield dbmod
    await dbmod.close_db()
