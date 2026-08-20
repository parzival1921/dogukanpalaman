"""Test ortamı: her test için izole, geçici bir veritabanı."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("PROVIDER", "demo")


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path):
    from app import db

    db_path = str(tmp_path / "test.db")
    db.reset_connection()
    db.connect(db_path)   # modül düzeyindeki bağlantıyı bu dosyaya sabitler
    yield db_path
    db.reset_connection()
