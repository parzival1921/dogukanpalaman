"""nalsesleri.com API istemcisi — atın geçmiş koşularından özellik üretir.

API (https://nalsesleri.com/apiservice):
    GET https://nalsesleri.com/api?horse_name=<at adı>
    -> {"status": "success", "data": [ <geçmiş koşu satırı>, ... ]}

Bu uç **atın geçmişini** verir; günün programını, start saatini ya da anlık
ganyanı vermez. Dolayısıyla tek başına kilitleme/sonuçlandırma döngüsünü
besleyemez; iki yerde kullanılır:

1. `EnrichedProvider` ile: başka bir kaynaktan gelen günün koşu kartındaki
   atların eksik alanlarını (form, dinlenme süresi, en iyi mesafe, pist tercihi,
   jokey isabet oranı) geçmişten doldurur.
2. `tools/build_dataset.py` ile: aynı (tarih, hipodrom, koşu no) anahtarını
   paylaşan satırlar birleştirilerek **geçmiş koşular yeniden kurulur** ve model
   simülasyon yerine gerçek veriyle geriye dönük test edilir.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import httpx

API_URL = "https://nalsesleri.com/api"

# Jokey isabet oranı için küçülterek (shrinkage) ortalamaya çekme:
# 1 koşuda 1 galibiyet %100 demek değildir.
LEAGUE_WIN_RATE = 0.10
SHRINKAGE_STARTS = 12.0


@dataclass(frozen=True)
class PastRun:
    """API'den gelen tek bir geçmiş koşu satırı."""

    race_date: date
    hippodrome_id: int
    race_number: int
    surface: str
    distance: int | None
    group_name: str
    horse_number: int | None
    lane: int | None
    horse_name: str
    jockey: str
    apprentice: bool
    weight: float | None
    non_runner: bool
    odds: float | None
    agf1: float | None
    agf2: float | None
    handicap: float | None
    result: int | None          # bitiş derecesi; 0/None = derece yok
    seconds: float | None       # "1:34.56" -> 94.56
    prize: float | None

    @property
    def race_key(self) -> str:
        return f"{self.race_date.isoformat()}-{self.hippodrome_id}-{self.race_number}"

    @property
    def finished(self) -> bool:
        return not self.non_runner and bool(self.result) and self.result > 0


def parse_seconds(value: Any) -> float | None:
    """"1:34.56" -> 94.56 · "58.20" -> 58.2 · boş -> None."""
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    match = re.fullmatch(r"(?:(\d+):)?(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    minutes = float(match.group(1) or 0)
    return minutes * 60.0 + float(match.group(2))


def _num(value: Any, cast=float):
    if value in (None, "", "-"):
        return None
    try:
        return cast(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return None


def parse_history(payload: Any) -> list[PastRun]:
    """API yanıtını PastRun listesine çevirir; en yeni koşu başta olacak şekilde sıralar."""
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return []

    runs: list[PastRun] = []
    for row in payload.get("data") or []:
        try:
            race_date = datetime.strptime(str(row["race_date"]).strip()[:10], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        runs.append(
            PastRun(
                race_date=race_date,
                hippodrome_id=int(_num(row.get("hippodrome_id"), int) or 0),
                race_number=int(_num(row.get("race_number"), int) or 0),
                surface=str(row.get("pist_type") or "").strip(),
                distance=_num(row.get("distance"), int),
                group_name=str(row.get("group_name") or "").strip(),
                horse_number=_num(row.get("horse_number"), int),
                lane=_num(row.get("lane"), int),
                horse_name=str(row.get("horse_name") or "").strip(),
                jockey=str(row.get("jockey") or "").strip(),
                apprentice=bool(_num(row.get("apprentice"), int) or 0),
                weight=_num(row.get("weight")),
                non_runner=bool(_num(row.get("non_runner"), int) or 0),
                odds=_num(row.get("odds")) or None,
                agf1=_num(row.get("AGF1")),
                agf2=_num(row.get("AGF2")),
                handicap=_num(row.get("handicap")),
                result=_num(row.get("result"), int),
                seconds=parse_seconds(row.get("time")),
                prize=_num(row.get("prize")),
            )
        )
    runs.sort(key=lambda r: (r.race_date, r.race_number), reverse=True)
    return runs


# --------------------------------------------------------------------------
# geçmişten özellik çıkarımı
# --------------------------------------------------------------------------

def form_positions(runs: Iterable[PastRun], limit: int = 6) -> list[int]:
    """Son N koşunun derecesi (yeni -> eski); koşmadığı koşular atlanır."""
    return [int(r.result) for r in runs if r.finished][:limit]


def days_since_run(runs: Iterable[PastRun], as_of: date) -> int | None:
    for run in runs:
        if run.non_runner or run.race_date > as_of:
            continue
        return (as_of - run.race_date).days
    return None


def best_distance(runs: Iterable[PastRun]) -> int | None:
    """En iyi derecesini yaptığı mesafe (eşitlikte daha hızlı olan)."""
    finished = [r for r in runs if r.finished and r.distance]
    if not finished:
        return None
    best = min(finished, key=lambda r: (r.result, -(r.seconds or 0) / max(r.distance or 1, 1)))
    return best.distance


def preferred_surface(runs: Iterable[PastRun]) -> str | None:
    """Ortalama derecesinin en iyi olduğu pist (en az 2 koşu), yoksa en sık koştuğu pist."""
    by_surface: dict[str, list[int]] = {}
    for run in runs:
        if run.finished and run.surface:
            by_surface.setdefault(run.surface, []).append(int(run.result))
    if not by_surface:
        return None
    qualified = {s: v for s, v in by_surface.items() if len(v) >= 2}
    pool = qualified or by_surface
    return min(pool, key=lambda s: sum(pool[s]) / len(pool[s]))


def jockey_win_rates(runs: Iterable[PastRun]) -> dict[str, float]:
    """Jokey bazında galibiyet oranı; az örnekli jokeyler lig ortalamasına çekilir."""
    tally: dict[str, list[int]] = {}
    for run in runs:
        if not run.jockey or not run.finished:
            continue
        stat = tally.setdefault(run.jockey, [0, 0])
        stat[0] += 1
        stat[1] += int(run.result == 1)
    return {
        jockey: (wins + SHRINKAGE_STARTS * LEAGUE_WIN_RATE) / (starts + SHRINKAGE_STARTS)
        for jockey, (starts, wins) in tally.items()
    }


def runner_profile(runs: list[PastRun], as_of: date) -> dict[str, Any]:
    """Bir atın geçmişinden model özelliklerini üretir."""
    return {
        "form": form_positions(runs),
        "days_since_run": days_since_run(runs, as_of),
        "best_distance": best_distance(runs),
        "surface_pref": preferred_surface(runs),
        "handicap": next((r.handicap for r in runs if r.handicap is not None), None),
        "weight": next((r.weight for r in runs if r.weight is not None), None),
    }


# --------------------------------------------------------------------------
# istemci
# --------------------------------------------------------------------------

class NalsesleriClient:
    """At geçmişi istemcisi; diske önbelleklenir ve eşzamanlı istek sayısı sınırlıdır."""

    def __init__(
        self,
        *,
        base_url: str = API_URL,
        cache_dir: str | Path | None = None,
        ttl_hours: float = 24.0,
        max_concurrency: int = 4,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ttl_seconds = ttl_hours * 3600
        self._sem = asyncio.Semaphore(max_concurrency)
        self._memory: dict[str, list[PastRun]] = {}
        self._client = client or httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"Accept": "application/json", "User-Agent": "atyarisi-dashboard/1.0"},
        )
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _slug(horse_name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", horse_name.strip().lower()).strip("-") or "at"

    def _cache_path(self, horse_name: str) -> Path | None:
        return self.cache_dir / f"{self._slug(horse_name)}.json" if self.cache_dir else None

    def _read_cache(self, horse_name: str) -> Any | None:
        path = self._cache_path(horse_name)
        if path and path.exists() and (time.time() - path.stat().st_mtime) < self.ttl_seconds:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
        return None

    def _write_cache(self, horse_name: str, payload: Any) -> None:
        path = self._cache_path(horse_name)
        if path:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    async def history(self, horse_name: str) -> list[PastRun]:
        """Atın geçmiş koşuları; bellek ve disk önbelleği kullanılır."""
        key = self._slug(horse_name)
        if key in self._memory:
            return self._memory[key]

        payload = self._read_cache(horse_name)
        if payload is None:
            async with self._sem:
                response = await self._client.get(self.base_url, params={"horse_name": horse_name})
                response.raise_for_status()
                payload = response.json()
            # "Veri bulunamadı" da geçerli bir yanıttır; tekrar tekrar sormamak için saklanır.
            self._write_cache(horse_name, payload)

        runs = parse_history(payload)
        self._memory[key] = runs
        return runs

    async def histories(self, horse_names: Iterable[str]) -> dict[str, list[PastRun]]:
        names = list(dict.fromkeys(n for n in horse_names if n))
        results = await asyncio.gather(
            *(self.history(name) for name in names), return_exceptions=True
        )
        output: dict[str, list[PastRun]] = {}
        for name, result in zip(names, results):
            output[name] = [] if isinstance(result, BaseException) else result
        return output

    async def aclose(self) -> None:
        await self._client.aclose()


# --------------------------------------------------------------------------
# geçmiş koşuların yeniden kurulması (gerçek veriyle backtest için)
# --------------------------------------------------------------------------

def reconstruct_races(
    histories: dict[str, list[PastRun]],
    *,
    min_field_size: int = 5,
) -> list[tuple["RaceCard", "RaceResult"]]:
    """Aynı (tarih, hipodrom, koşu no) satırlarını birleştirip koşuları yeniden kurar.

    Geleceğe bakma (look-ahead) yok: her atın form/dinlenme/mesafe profili ve her
    jokeyin isabet oranı, YALNIZCA o koşudan ÖNCEKİ satırlardan hesaplanır. Aksi
    halde backtest, o gün bilinemeyecek bilgiyi kullanır ve sonuç şişer.
    """
    from ..models import RaceCard, RaceResult, Runner

    by_horse: dict[str, list[PastRun]] = {
        name: sorted(runs, key=lambda r: r.race_date) for name, runs in histories.items()
    }
    groups: dict[str, list[PastRun]] = {}
    for runs in histories.values():
        for run in runs:
            groups.setdefault(run.race_key, []).append(run)

    # Koşular kronolojik işlenir; jokey istatistiği koşudan sonra güncellenir.
    ordered = sorted(groups.items(), key=lambda kv: (kv[1][0].race_date, kv[1][0].race_number))
    tally: dict[str, list[int]] = {}

    def rate(jockey: str) -> float | None:
        stat = tally.get(jockey)
        if not stat:
            return None
        starts, wins = stat
        return (wins + SHRINKAGE_STARTS * LEAGUE_WIN_RATE) / (starts + SHRINKAGE_STARTS)

    output: list[tuple[RaceCard, RaceResult]] = []
    for race_key, rows in ordered:
        head = rows[0]
        runners: list[Runner] = []
        for row in rows:
            if row.horse_number is None:
                continue
            # Sadece bu koşudan önceki satırlar.
            earlier = [r for r in by_horse.get(row.horse_name, []) if r.race_date < head.race_date]
            profile = runner_profile(list(reversed(earlier)), head.race_date)
            runners.append(
                Runner(
                    number=int(row.horse_number),
                    horse=row.horse_name,
                    jockey=row.jockey,
                    weight=row.weight,
                    handicap=row.handicap,
                    form=profile["form"],
                    draw=row.lane,
                    days_since_run=profile["days_since_run"],
                    best_distance=profile["best_distance"],
                    surface_pref=profile["surface_pref"],
                    jockey_win_rate=rate(row.jockey),
                    agf=row.agf1,
                    odds=row.odds,
                    scratched=row.non_runner,
                )
            )

        for row in rows:                       # istatistik koşudan SONRA güncellenir
            if row.jockey and row.finished:
                stat = tally.setdefault(row.jockey, [0, 0])
                stat[0] += 1
                stat[1] += int(row.result == 1)

        finishers = sorted(
            (r for r in rows if r.finished and r.horse_number is not None),
            key=lambda r: r.result,
        )
        active = [r for r in runners if not r.scratched]
        if len(active) < min_field_size or not finishers:
            continue

        card = RaceCard(
            id=race_key,
            track=f"Hipodrom {head.hippodrome_id}",
            race_no=head.race_number,
            race_date=head.race_date.isoformat(),
            post_time=datetime.combine(head.race_date, datetime.min.time()).replace(
                tzinfo=__import__("datetime").timezone.utc
            ),
            distance=head.distance,
            surface=head.surface,
            race_class=head.group_name,
            runners=runners,
        )
        winner = finishers[0]
        result = RaceResult(
            race_id=race_key,
            finish_order=[int(r.horse_number) for r in finishers],
            win_dividends={int(winner.horse_number): winner.odds} if winner.odds else {},
        )
        output.append((card, result))
    return output
