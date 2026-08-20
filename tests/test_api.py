"""HTTP API testleri (uygulama yaşam döngüsü ile birlikte)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["last_error"] is None
    assert body["bankroll"] > 0


def test_dashboard_endpoint(client):
    body = client.get("/api/dashboard").json()
    assert "stats" in body and "upcoming" in body and "results" in body
    assert body["lock_minutes"] == 5.0


def test_index_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "At Yarışı Tahmin Dashboard" in response.text


def test_race_detail_and_bets(client):
    races = client.get("/api/races").json()
    assert races, "demo sağlayıcı en az bir koşu üretmeli"

    detail = client.get(f"/api/races/{races[0]['id']}").json()
    assert detail["race"]["runners"]
    assert "prediction" in detail

    assert client.get("/api/races/yok-boyle-kosu").status_code == 404
    assert isinstance(client.get("/api/bets").json(), list)


def test_manual_sync(client):
    body = client.post("/api/sync").json()
    assert set(body) == {"races", "odds", "locked", "settled"}
