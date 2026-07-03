import sys
from pathlib import Path

import pytest

# Make `backend` importable regardless of how pytest is invoked
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Point the app's SQLite database at a per-test temp file."""
    import backend.db as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", str(tmp_path / "test.db"))
    return dbmod
