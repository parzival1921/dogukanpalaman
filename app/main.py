"""FastAPI uygulaması: dashboard ve JSON API."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, service
from .config import settings
from .scheduler import Scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s · %(message)s",
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

scheduler: Scheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    db.connect()
    scheduler = Scheduler()
    await scheduler.tick()      # ilk veriyi hemen doldur
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(title="At Yarışı Tahmin Dashboard", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/dashboard")
async def api_dashboard() -> dict:
    return service.dashboard()


@app.get("/api/stats")
async def api_stats() -> dict:
    return service.stats()


@app.get("/api/races")
async def api_races() -> list[dict]:
    return service.upcoming_races(limit=50)


@app.get("/api/races/{race_id}")
async def api_race(race_id: str) -> dict:
    row = db.get_race(race_id)
    if row is None:
        raise HTTPException(status_code=404, detail="koşu bulunamadı")

    race = service.row_to_race(row)
    prediction = db.get_prediction(race_id)
    payload = {
        "race": {
            **service._fmt_race(row),
            "purse": row["purse"],
            "runners": [r.to_dict() for r in race.runners],
        },
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "bets": [service._fmt_bet(b) for b in db.list_bets(race_id=race_id)],
    }
    if prediction:
        payload["prediction"] = {
            "locked_at": prediction["locked_at"],
            "minutes_to_post": prediction["minutes_to_post"],
            "status": prediction["status"],
            "profit": prediction["profit"],
            "assessments": json.loads(prediction["assessments_json"]),
        }
    else:
        payload["prediction"] = None
        payload["live"] = service.preview(race)
    return payload


@app.get("/api/bets")
async def api_bets(status: str | None = None, limit: int = 200) -> list[dict]:
    return [service._fmt_bet(b) for b in db.list_bets(status=status, limit=limit)]


@app.get("/api/results")
async def api_results(limit: int = 50) -> list[dict]:
    return service.settled_results(limit=limit)


@app.get("/api/ledger")
async def api_ledger(limit: int = 200) -> list[dict]:
    return [dict(row) for row in db.list_ledger(limit=limit)]


@app.post("/api/sync")
async def api_sync() -> dict:
    """Döngüyü elle tetikler (program, oran, kilit, sonuç)."""
    if scheduler is None:
        raise HTTPException(status_code=503, detail="zamanlayıcı hazır değil")
    return await scheduler.tick()


@app.get("/api/health")
async def api_health() -> dict:
    return {
        "status": "ok",
        "provider": settings.provider,
        "last_tick": db.iso(scheduler.last_tick) if scheduler and scheduler.last_tick else None,
        "last_error": scheduler.last_error if scheduler else None,
        "bankroll": db.bankroll(),
    }
