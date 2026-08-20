"""Genel amaçlı JSON API sağlayıcısı.

Herhangi bir REST/JSON kaynağını (TJK ajax uçları, RapidAPI at yarışı servisleri,
kendi backend'iniz...) kod yazmadan bağlamak için bir eşleme (mapping) dosyası
kullanır. Eşleme dosyasının şeması `config/provider.example.json` içinde
açıklanmıştır.

Alan eşlemesi biçimleri:
    "horseName"                                  -> düz nokta yolu
    {"path": "kilo", "type": "float"}            -> tip dönüşümü
    {"path": "start", "type": "datetime",
     "format": "%d/%m/%Y %H:%M", "tz": "Europe/Istanbul"}
    {"const": "kum"}                             -> sabit değer
    {"path": "sonuclar", "type": "int_list"}     -> liste
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..models import RaceCard, RaceResult, Runner

_TOKEN_RE = re.compile(r"\{date(?::([^}]+))?\}|\{race_id\}|\{race_no\}|\{track\}")


def _dig(data: Any, path: str) -> Any:
    """Nokta ile ayrılmış yol: "a.b.0.c" (liste indeksleri de desteklenir)."""
    if path in ("", "."):
        return data
    current = data
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _coerce(value: Any, spec: dict[str, Any]) -> Any:
    kind = spec.get("type", "str")
    if value is None:
        return spec.get("default")
    try:
        if kind == "str":
            return str(value).strip()
        if kind == "int":
            return int(float(str(value).replace(",", ".")))
        if kind == "float":
            return float(str(value).replace(",", "."))
        if kind == "bool":
            return str(value).strip().lower() in ("1", "true", "yes", "evet", "e", "x")
        if kind == "int_list":
            if isinstance(value, str):
                value = re.split(r"[^0-9]+", value)
            return [int(v) for v in value if str(v).strip().isdigit()]
        if kind == "datetime":
            return _parse_datetime(value, spec)
        if kind == "map_number_float":
            # {"3": "4,15"} -> {3: 4.15}
            return {int(k): float(str(v).replace(",", ".")) for k, v in dict(value).items()}
    except (TypeError, ValueError):
        return spec.get("default")
    return value


def _parse_datetime(value: Any, spec: dict[str, Any]) -> datetime | None:
    tz = ZoneInfo(spec.get("tz", "Europe/Istanbul"))
    fmt = spec.get("format")
    text = str(value).strip()
    dt: datetime | None = None
    if fmt:
        dt = datetime.strptime(text, fmt)
    else:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def _field(item: dict[str, Any], spec: Any, context: dict[str, Any] | None = None) -> Any:
    if spec is None:
        return None
    if isinstance(spec, str):
        spec = {"path": spec}
    if "const" in spec:
        return spec["const"]
    if "context" in spec:
        return (context or {}).get(spec["context"])
    raw = _dig(item, spec.get("path", ""))
    return _coerce(raw, spec)


def _render(template: str, *, day: date | None = None, race: RaceCard | None = None) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith("{date"):
            fmt = match.group(1) or "%Y-%m-%d"
            return (day or date.today()).strftime(fmt)
        if token == "{race_id}":
            return race.id if race else ""
        if token == "{race_no}":
            return str(race.race_no) if race else ""
        if token == "{track}":
            return race.track if race else ""
        return token

    return _TOKEN_RE.sub(repl, template)


class HttpJsonProvider:
    """Eşleme dosyasıyla yapılandırılan JSON API sağlayıcısı."""

    def __init__(self, config: dict[str, Any], client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.name = config.get("name", "http")
        self._client = client or httpx.AsyncClient(
            base_url=config.get("base_url", ""),
            headers=config.get("headers", {}),
            timeout=float(config.get("timeout_seconds", 15)),
            follow_redirects=True,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "HttpJsonProvider":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data)

    # ------------------------------------------------------------------
    async def _request(
        self,
        section: dict[str, Any],
        *,
        day: date | None = None,
        race: RaceCard | None = None,
    ) -> Any:
        url = _render(section.get("path", "/"), day=day, race=race)
        params = {k: _render(str(v), day=day, race=race) for k, v in (section.get("params") or {}).items()}
        method = section.get("method", "GET").upper()
        body = section.get("json")
        response = await self._client.request(method, url, params=params, json=body)
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------
    async def fetch_race_cards(self, day: date) -> list[RaceCard]:
        section = self.config.get("race_cards")
        if not section:
            return []
        payload = await self._request(section, day=day)
        items = _dig(payload, section.get("items", "")) or []
        mapping = section.get("map", {})
        runner_section = section.get("runners", {})
        runner_map = runner_section.get("map", {})

        cards: list[RaceCard] = []
        for item in items:
            post_time = _field(item, mapping.get("post_time"))
            if not isinstance(post_time, datetime):
                continue
            race_id = _field(item, mapping.get("id"))
            if race_id is None:
                continue

            runners_raw = _dig(item, runner_section.get("items", "runners")) or []
            runners = []
            for raw in runners_raw:
                number = _field(raw, runner_map.get("number"))
                if number is None:
                    continue
                runners.append(
                    Runner(
                        number=int(number),
                        horse=str(_field(raw, runner_map.get("horse")) or f"At {number}"),
                        jockey=str(_field(raw, runner_map.get("jockey")) or ""),
                        trainer=str(_field(raw, runner_map.get("trainer")) or ""),
                        age=_field(raw, runner_map.get("age")),
                        weight=_field(raw, runner_map.get("weight")),
                        handicap=_field(raw, runner_map.get("handicap")),
                        form=_field(raw, runner_map.get("form")) or [],
                        draw=_field(raw, runner_map.get("draw")),
                        days_since_run=_field(raw, runner_map.get("days_since_run")),
                        best_distance=_field(raw, runner_map.get("best_distance")),
                        surface_pref=_field(raw, runner_map.get("surface_pref")),
                        jockey_win_rate=_field(raw, runner_map.get("jockey_win_rate")),
                        agf=_field(raw, runner_map.get("agf")),
                        odds=_field(raw, runner_map.get("odds")),
                        scratched=bool(_field(raw, runner_map.get("scratched")) or False),
                    )
                )

            cards.append(
                RaceCard(
                    id=str(race_id),
                    track=str(_field(item, mapping.get("track")) or "?"),
                    race_no=int(_field(item, mapping.get("race_no")) or 0),
                    race_date=post_time.date().isoformat(),
                    post_time=post_time,
                    distance=_field(item, mapping.get("distance")),
                    surface=_field(item, mapping.get("surface")),
                    race_class=_field(item, mapping.get("race_class")),
                    purse=_field(item, mapping.get("purse")),
                    runners=runners,
                )
            )
        return cards

    async def fetch_odds(self, race: RaceCard) -> dict[int, float]:
        section = self.config.get("odds")
        if not section:
            return {}
        payload = await self._request(section, race=race)

        direct = section.get("map_field")
        if direct:
            value = _dig(payload, direct)
            if isinstance(value, dict):
                return {int(k): float(str(v).replace(",", ".")) for k, v in value.items()}

        items = _dig(payload, section.get("items", "")) or []
        mapping = section.get("map", {})
        quotes: dict[int, float] = {}
        for raw in items:
            number = _field(raw, mapping.get("number"))
            odds = _field(raw, mapping.get("odds"))
            if number is None or odds in (None, 0):
                continue
            quotes[int(number)] = float(odds)
        return quotes

    async def fetch_result(self, race: RaceCard) -> RaceResult | None:
        section = self.config.get("results")
        if not section:
            return None
        payload = await self._request(section, race=race)
        mapping = section.get("map", {})

        root = _dig(payload, section.get("root", "")) if section.get("root") else payload
        if root is None:
            return None

        finish_order = _field(root, mapping.get("finish_order"))
        if not finish_order:
            items = _dig(root, section.get("items", "")) or []
            runner_map = section.get("runner_map", {})
            ranked = []
            for raw in items:
                number = _field(raw, runner_map.get("number"))
                position = _field(raw, runner_map.get("position"))
                if number is None or position is None:
                    continue
                ranked.append((int(position), int(number)))
            ranked.sort()
            finish_order = [n for _, n in ranked]

        if not finish_order:
            return None

        win_div = _field(root, mapping.get("win_dividends")) or {}
        place_div = _field(root, mapping.get("place_dividends")) or {}
        return RaceResult(
            race_id=race.id,
            finish_order=[int(n) for n in finish_order],
            win_dividends={int(k): float(v) for k, v in dict(win_div).items()},
            place_dividends={int(k): float(v) for k, v in dict(place_div).items()},
            cancelled=bool(_field(root, mapping.get("cancelled")) or False),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
