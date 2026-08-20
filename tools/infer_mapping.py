"""Kaydedilmiş bir API yanıtından sağlayıcı eşlemesini otomatik çıkarır.

Kullanım:
    python -m tools.infer_mapping program.json                 # koşu programı
    python -m tools.infer_mapping ganyan.json --section odds    # oranlar
    python -m tools.infer_mapping sonuc.json  --section results # sonuçlar

Yaptığı iş: JSON içindeki kayıt listelerini bulur, anahtar adlarını Türkçe/İngilizce
eş anlamlı sözlüğüyle eşleştirir, örnek değerlerden tipi (ve tarih biçimini) tahmin
eder ve `config/provider.*.json` dosyasına yapıştırılabilir bir eşleme basar.

Tahmin kesin değildir: çıktıyı gözden geçirin, eşleşmeyen alanları elle düzeltin,
sonra `python -m tools.check_provider <config> --sample <dosya>` ile doğrulayın.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Türkçe karakterler sadeleştirilir: "kosuSaati" ve "koşuSaati" aynı sayılır.
_TR = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def normalize(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.translate(_TR).lower())


# Alan -> eş anlamlılar (normalize edilmiş). Sıra önemli değil; skor belirler.
RACE_FIELDS: dict[str, list[str]] = {
    "id": ["kosukodu", "kosuid", "raceid", "id", "kod", "kosuno_unique"],
    "track": ["hipodrom", "hipodromadi", "sehir", "sehiradi", "yer", "track", "course", "venue"],
    "race_no": ["kosuno", "kosusira", "raceno", "racenumber", "kosunumarasi", "sira"],
    "post_time": ["kosusaati", "saat", "start", "starttime", "posttime", "offtime", "tarihsaat", "time", "datetime"],
    "distance": ["mesafe", "distance"],
    "surface": ["pist", "pisttipi", "zemin", "going", "surface"],
    "race_class": ["kosucinsi", "cins", "sinif", "grup", "class", "raceclass", "kategori"],
    "purse": ["ikramiye", "prize", "purse", "odul"],
}

RUNNER_FIELDS: dict[str, list[str]] = {
    "number": ["atno", "no", "number", "startno", "kosuno", "saddle", "numara"],
    "horse": ["atadi", "at", "horse", "horsename", "isim", "ad", "name"],
    "jockey": ["jokey", "jokeyadi", "jockey", "rider", "binici"],
    "trainer": ["antrenor", "antrenoradi", "trainer", "sahibi", "sahip"],
    "age": ["yas", "age"],
    "weight": ["kilo", "siklet", "weight", "tasidigikilo"],
    "handicap": ["handikap", "handikappuani", "hp", "rating", "puan"],
    "form": ["son6", "son5", "son4", "sondereceler", "form", "lastruns", "gecmis"],
    "draw": ["startyeri", "kulvar", "draw", "gate", "box"],
    "days_since_run": ["sonkosugun", "sonkosudangecengun", "gun", "dayssincerun", "dslr"],
    "best_distance": ["eniyiderecemesafe", "bestdistance", "eniyimesafe"],
    "surface_pref": ["tercihpist", "favoripist", "preferredgoing", "sevdigipist"],
    "jockey_win_rate": ["jokeykazanmaorani", "jokeyoran", "strikerate", "winrate", "jokeyyuzde"],
    "agf": ["agf", "agf1", "agforan", "halkoran", "publicpct"],
    "odds": ["ganyan", "ganyanoran", "oran", "odds", "muhtemel", "win", "songanyan"],
    "scratched": ["kosmaz", "cekildi", "cikmadi", "nonrunner", "scratched", "iptal"],
    "position": ["derece", "sira", "position", "finish", "pos", "sonuc"],
}

# Alan -> tercih edilen tip.
PREFERRED_TYPE = {
    "race_no": "int", "distance": "int", "purse": "float", "post_time": "datetime",
    "number": "int", "age": "int", "weight": "float", "handicap": "float",
    "form": "int_list", "draw": "int", "days_since_run": "int", "best_distance": "int",
    "jockey_win_rate": "float", "agf": "float", "odds": "float", "scratched": "bool",
    "position": "int",
}

DATE_FORMATS = [
    "%d/%m/%Y %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%H:%M", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d",
]


def score(key: str, synonyms: list[str]) -> int:
    """Anahtar adının bir alana ne kadar uyduğu (0 = uymuyor)."""
    n = normalize(key)
    if not n:
        return 0
    best = 0
    for syn in synonyms:
        if n == syn:
            best = max(best, 100)
        elif n.startswith(syn) or n.endswith(syn):
            best = max(best, 60 + len(syn))
        elif syn in n:
            best = max(best, 30 + len(syn))
    return best


def guess_datetime_format(value: Any) -> str | None:
    text = str(value).strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return None          # ISO 8601 -> format gerekmez
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(text, fmt)
            return fmt
        except ValueError:
            continue
    return "?"               # tanınmadı, elle yazılmalı


def build_spec(field: str, key: str, sample: Any) -> Any:
    kind = PREFERRED_TYPE.get(field)
    if kind is None:
        return key                                    # düz str -> kısa gösterim
    if kind == "datetime":
        spec: dict[str, Any] = {"path": key, "type": "datetime", "tz": "Europe/Istanbul"}
        fmt = guess_datetime_format(sample)
        if fmt:
            spec["format"] = fmt
        return spec
    if kind == "bool":
        return {"path": key, "type": "bool", "default": False}
    return {"path": key, "type": kind}


def match_fields(item: dict[str, Any], fields: dict[str, list[str]]) -> dict[str, Any]:
    """Kayıttaki anahtarları alanlara açgözlü (en yüksek skor önce) eşler."""
    pairs = []
    for field, synonyms in fields.items():
        for key in item:
            s = score(key, synonyms)
            if s:
                pairs.append((s, field, key))
    pairs.sort(reverse=True)

    mapping: dict[str, Any] = {}
    used_keys: set[str] = set()
    for _, field, key in pairs:
        if field in mapping or key in used_keys:
            continue
        mapping[field] = build_spec(field, key, item.get(key))
        used_keys.add(key)
    return mapping


def find_record_lists(data: Any, path: str = "") -> list[tuple[str, list]]:
    """JSON içindeki tüm "sözlük listesi" düğümlerini yol bilgisiyle döndürür."""
    found: list[tuple[str, list]] = []
    if isinstance(data, list):
        if data and all(isinstance(x, dict) for x in data):
            found.append((path, data))
        for idx, item in enumerate(data[:1]):
            found.extend(find_record_lists(item, f"{path}.{idx}" if path else str(idx)))
    elif isinstance(data, dict):
        for key, value in data.items():
            found.extend(find_record_lists(value, f"{path}.{key}" if path else key))
    return found


def rank_candidates(candidates: list[tuple[str, list]], fields: dict[str, list[str]]) -> list[tuple[int, str, list]]:
    ranked = []
    for path, items in candidates:
        matched = match_fields(items[0], fields)
        ranked.append((len(matched), path, items))
    ranked.sort(key=lambda r: (-r[0], len(r[1])))
    return ranked


def infer_race_cards(payload: Any) -> dict[str, Any]:
    candidates = find_record_lists(payload)
    if not candidates:
        raise SystemExit("JSON içinde kayıt listesi bulunamadı — dosya doğru mu?")

    ranked = rank_candidates(candidates, RACE_FIELDS)
    hits, path, items = ranked[0]
    print(f"# koşu listesi: items = \"{path}\"  ({len(items)} kayıt, {hits} alan eşleşti)", file=sys.stderr)

    race_map = match_fields(items[0], RACE_FIELDS)

    # Koşu kaydının içindeki at listesini bul.
    inner = [(p, v) for p, v in find_record_lists(items[0]) if p]
    runners: dict[str, Any] = {}
    if inner:
        r_hits, r_path, r_items = rank_candidates(inner, RUNNER_FIELDS)[0]
        print(f"# at listesi  : runners.items = \"{r_path}\"  ({len(r_items)} at, {r_hits} alan eşleşti)", file=sys.stderr)
        runners = {"items": r_path, "map": match_fields(r_items[0], RUNNER_FIELDS)}
    else:
        print("# UYARI: koşu kaydının içinde at listesi bulunamadı", file=sys.stderr)

    return {
        "path": "/DEĞİŞTİRİN",
        "method": "GET",
        "params": {"tarih": "{date:%d/%m/%Y}"},
        "items": path,
        "map": race_map,
        "runners": runners,
    }


def infer_simple(payload: Any, fields: list[str]) -> dict[str, Any]:
    candidates = find_record_lists(payload)
    if not candidates:
        raise SystemExit("JSON içinde kayıt listesi bulunamadı — dosya doğru mu?")
    subset = {k: RUNNER_FIELDS[k] for k in fields}
    hits, path, items = rank_candidates(candidates, subset)[0]
    print(f"# liste: items = \"{path}\"  ({len(items)} kayıt, {hits} alan eşleşti)", file=sys.stderr)
    return {"path": "/DEĞİŞTİRİN", "items": path, "map": match_fields(items[0], subset)}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    file_path = Path(argv[0])
    section = "race_cards"
    if "--section" in argv:
        section = argv[argv.index("--section") + 1]

    payload = json.loads(file_path.read_text(encoding="utf-8"))

    if section == "race_cards":
        result = {"race_cards": infer_race_cards(payload)}
    elif section == "odds":
        result = {"odds": infer_simple(payload, ["number", "odds"])}
    elif section == "results":
        inferred = infer_simple(payload, ["number", "position"])
        result = {
            "results": {
                "path": "/DEĞİŞTİRİN",
                "items": inferred["items"],
                "runner_map": inferred["map"],
                "map": {"cancelled": {"path": "iptal", "type": "bool", "default": False}},
            }
        }
    else:
        raise SystemExit(f"bilinmeyen bölüm: {section} (race_cards|odds|results)")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
