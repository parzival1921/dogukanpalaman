"""SQLite kalıcılık katmanı.

Tahminler kilitlendikten sonra DEĞİŞTİRİLEMEZ: `predictions` ve `bets` tabloları
üzerinde UPDATE yalnızca sonuçlandırma (settle) alanlarına izin verir; bunu
uygulama seviyesinde `lock_prediction()` / `settle_bet()` fonksiyonları ve
veritabanı seviyesinde bir tetikleyici (trigger) birlikte garanti eder.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    id            TEXT PRIMARY KEY,
    track         TEXT NOT NULL,
    race_no       INTEGER NOT NULL,
    race_date     TEXT NOT NULL,
    post_time     TEXT NOT NULL,          -- ISO 8601, UTC
    distance      INTEGER,
    surface       TEXT,
    race_class    TEXT,
    purse         REAL,
    runners_json  TEXT NOT NULL DEFAULT '[]',
    result_json   TEXT,
    status        TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled|locked|finished|cancelled
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_races_post_time ON races(post_time);

CREATE TABLE IF NOT EXISTS odds_history (
    race_id   TEXT NOT NULL,
    number    INTEGER NOT NULL,
    odds      REAL NOT NULL,
    ts        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_odds_race ON odds_history(race_id, ts);

CREATE TABLE IF NOT EXISTS predictions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id           TEXT NOT NULL UNIQUE REFERENCES races(id),
    locked_at         TEXT NOT NULL,
    post_time         TEXT NOT NULL,
    minutes_to_post   REAL NOT NULL,
    bankroll_at_lock  REAL NOT NULL,
    assessments_json  TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'locked',   -- locked|settled|void
    settled_at        TEXT,
    profit            REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id  INTEGER NOT NULL REFERENCES predictions(id),
    race_id        TEXT NOT NULL REFERENCES races(id),
    number         INTEGER NOT NULL,
    horse          TEXT NOT NULL,
    bet_type       TEXT NOT NULL,
    odds_at_lock   REAL NOT NULL,
    model_prob     REAL NOT NULL,
    market_prob    REAL NOT NULL,
    edge           REAL NOT NULL,
    ev             REAL NOT NULL,
    stake          REAL NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',   -- pending|won|lost|void
    payout         REAL NOT NULL DEFAULT 0,
    profit         REAL NOT NULL DEFAULT 0,
    finish_pos     INTEGER,
    dividend       REAL,
    settled_at     TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bets_race ON bets(race_id);
CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);

CREATE TABLE IF NOT EXISTS ledger (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    event    TEXT NOT NULL,
    race_id  TEXT,
    amount   REAL NOT NULL,
    balance  REAL NOT NULL,
    note     TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Kilitlenmiş tahmin tekrar yazılamaz: yalnızca settle alanları güncellenebilir.
CREATE TRIGGER IF NOT EXISTS trg_predictions_immutable
BEFORE UPDATE ON predictions
FOR EACH ROW WHEN
    OLD.race_id          <> NEW.race_id
 OR OLD.locked_at        <> NEW.locked_at
 OR OLD.assessments_json <> NEW.assessments_json
 OR OLD.bankroll_at_lock <> NEW.bankroll_at_lock
BEGIN
    SELECT RAISE(ABORT, 'kilitli tahmin degistirilemez');
END;

CREATE TRIGGER IF NOT EXISTS trg_bets_immutable
BEFORE UPDATE ON bets
FOR EACH ROW WHEN
    OLD.race_id      <> NEW.race_id
 OR OLD.number       <> NEW.number
 OR OLD.bet_type     <> NEW.bet_type
 OR OLD.odds_at_lock <> NEW.odds_at_lock
 OR OLD.stake        <> NEW.stake
 OR OLD.model_prob   <> NEW.model_prob
BEGIN
    SELECT RAISE(ABORT, 'kilitli bahis degistirilemez');
END;
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def connect(path: str | None = None) -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is not None and path is None:
            return _CONN
        db_path = path or settings.database_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
        if path is None or _CONN is None:
            _CONN = conn
        return conn


def reset_connection() -> None:
    """Testler için: açık bağlantıyı kapat."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
        _CONN = None


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    conn = connect()
    with _LOCK:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------

def get_meta(key: str, default: str | None = None) -> str | None:
    with cursor() as cur:
        row = cur.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --------------------------------------------------------------------------
# races
# --------------------------------------------------------------------------

def upsert_race(race: dict[str, Any]) -> None:
    now = iso(utcnow())
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO races (id, track, race_no, race_date, post_time, distance, surface,
                               race_class, purse, runners_json, status, created_at, updated_at)
            VALUES (:id, :track, :race_no, :race_date, :post_time, :distance, :surface,
                    :race_class, :purse, :runners_json, 'scheduled', :now, :now)
            ON CONFLICT(id) DO UPDATE SET
                track        = excluded.track,
                race_no      = excluded.race_no,
                race_date    = excluded.race_date,
                post_time    = excluded.post_time,
                distance     = excluded.distance,
                surface      = excluded.surface,
                race_class   = excluded.race_class,
                purse        = excluded.purse,
                runners_json = excluded.runners_json,
                updated_at   = excluded.updated_at
            """,
            {**race, "now": now},
        )


def set_race_status(race_id: str, status: str) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE races SET status = ?, updated_at = ? WHERE id = ?",
            (status, iso(utcnow()), race_id),
        )


def save_race_result(race_id: str, result: dict[str, Any], status: str) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE races SET result_json = ?, status = ?, updated_at = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False), status, iso(utcnow()), race_id),
        )


def get_race(race_id: str) -> sqlite3.Row | None:
    with cursor() as cur:
        return cur.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()


def list_races(
    *,
    after: datetime | None = None,
    before: datetime | None = None,
    status: str | None = None,
) -> list[sqlite3.Row]:
    clauses, params = [], []
    if after is not None:
        clauses.append("post_time >= ?")
        params.append(iso(after))
    if before is not None:
        clauses.append("post_time <= ?")
        params.append(iso(before))
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with cursor() as cur:
        return cur.execute(f"SELECT * FROM races {where} ORDER BY post_time ASC", params).fetchall()


def record_odds(race_id: str, quotes: dict[int, float]) -> None:
    ts = iso(utcnow())
    with cursor() as cur:
        cur.executemany(
            "INSERT INTO odds_history(race_id, number, odds, ts) VALUES (?, ?, ?, ?)",
            [(race_id, n, o, ts) for n, o in quotes.items()],
        )


# --------------------------------------------------------------------------
# predictions & bets
# --------------------------------------------------------------------------

def get_prediction(race_id: str) -> sqlite3.Row | None:
    with cursor() as cur:
        return cur.execute("SELECT * FROM predictions WHERE race_id = ?", (race_id,)).fetchone()


def has_prediction(race_id: str) -> bool:
    return get_prediction(race_id) is not None


def insert_prediction(
    *,
    race_id: str,
    locked_at: datetime,
    post_time: datetime,
    bankroll: float,
    assessments: list[dict[str, Any]],
    bets: list[dict[str, Any]],
) -> int:
    """Tahmini ve seçilen bahisleri tek seferde ve değiştirilemez şekilde yazar."""
    minutes = (post_time - locked_at).total_seconds() / 60.0
    now = iso(utcnow())
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO predictions (race_id, locked_at, post_time, minutes_to_post,
                                     bankroll_at_lock, assessments_json, status)
            VALUES (?, ?, ?, ?, ?, ?, 'locked')
            """,
            (
                race_id,
                iso(locked_at),
                iso(post_time),
                round(minutes, 2),
                bankroll,
                json.dumps(assessments, ensure_ascii=False),
            ),
        )
        prediction_id = int(cur.lastrowid)
        for bet in bets:
            cur.execute(
                """
                INSERT INTO bets (prediction_id, race_id, number, horse, bet_type, odds_at_lock,
                                  model_prob, market_prob, edge, ev, stake, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    prediction_id,
                    race_id,
                    bet["number"],
                    bet["horse"],
                    bet["bet_type"],
                    bet["odds"],
                    bet["model_prob"],
                    bet["market_prob"],
                    bet["edge"],
                    bet["ev"],
                    bet["stake"],
                    now,
                ),
            )
    return prediction_id


def list_predictions(status: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
    where = "WHERE p.status = ?" if status else ""
    params = [status] if status else []
    with cursor() as cur:
        return cur.execute(
            f"""
            SELECT p.*, r.track, r.race_no, r.race_date, r.distance, r.surface, r.result_json
            FROM predictions p JOIN races r ON r.id = p.race_id
            {where}
            ORDER BY p.post_time DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()


def list_bets(race_id: str | None = None, status: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
    clauses, params = [], []
    if race_id:
        clauses.append("b.race_id = ?")
        params.append(race_id)
    if status:
        clauses.append("b.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with cursor() as cur:
        return cur.execute(
            f"""
            SELECT b.*, r.track, r.race_no, r.post_time, r.race_date
            FROM bets b JOIN races r ON r.id = b.race_id
            {where}
            ORDER BY r.post_time DESC, b.id ASC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()


def settle_bet(
    bet_id: int,
    *,
    status: str,
    payout: float,
    profit: float,
    finish_pos: int | None,
    dividend: float | None,
) -> None:
    with cursor() as cur:
        cur.execute(
            """
            UPDATE bets
            SET status = ?, payout = ?, profit = ?, finish_pos = ?, dividend = ?, settled_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (status, payout, profit, finish_pos, dividend, iso(utcnow()), bet_id),
        )


def close_prediction(prediction_id: int, *, status: str, profit: float) -> None:
    with cursor() as cur:
        cur.execute(
            "UPDATE predictions SET status = ?, profit = ?, settled_at = ? WHERE id = ?",
            (status, profit, iso(utcnow()), prediction_id),
        )


def list_pending_predictions() -> list[sqlite3.Row]:
    with cursor() as cur:
        return cur.execute(
            "SELECT * FROM predictions WHERE status = 'locked' ORDER BY post_time ASC"
        ).fetchall()


# --------------------------------------------------------------------------
# bankroll / ledger
# --------------------------------------------------------------------------

def bankroll() -> float:
    with cursor() as cur:
        row = cur.execute("SELECT balance FROM ledger ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return settings.bankroll_start
    return float(row["balance"])


def add_ledger(event: str, amount: float, *, race_id: str | None = None, note: str | None = None) -> float:
    with cursor() as cur:
        row = cur.execute("SELECT balance FROM ledger ORDER BY id DESC LIMIT 1").fetchone()
        balance = float(row["balance"]) if row else settings.bankroll_start
        balance = round(balance + amount, 2)
        cur.execute(
            "INSERT INTO ledger(ts, event, race_id, amount, balance, note) VALUES (?, ?, ?, ?, ?, ?)",
            (iso(utcnow()), event, race_id, round(amount, 2), balance, note),
        )
    return balance


def list_ledger(limit: int = 500) -> list[sqlite3.Row]:
    with cursor() as cur:
        return cur.execute(
            "SELECT * FROM ledger ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
