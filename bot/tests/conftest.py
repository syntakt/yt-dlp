"""Keep persistent state isolated for every regression test."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'telegram_bot'))


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    import database
    database.close_connection()
    monkeypatch.setattr(database, 'DB_PATH', tmp_path / 'state' / 'bot.db')
    database.init_db()
    yield
    database.close_connection()
