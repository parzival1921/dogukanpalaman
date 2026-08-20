"""At adlarından geçmiş koşu veri seti kurar (nalsesleri.com API'si).

Kullanım:
    python -m tools.build_dataset atlar.txt dataset.json

`atlar.txt`: her satırda bir at adı. Her at için API'ye tek istek atılır, yanıtlar
diske önbelleklenir (tekrar çalıştırınca yeniden indirilmez).

Sonra `python -m tools.backtest --dataset dataset.json` ile model gerçek veriyle
test edilir.

ÖNEMLİ: API yalnızca sorguladığınız atın satırlarını döndürür. Bir koşunun tamamını
yeniden kurabilmek için o koşudaki atların TAMAMINI sorgulamış olmanız gerekir;
eksik kadrolu koşular `--min-field` altında kalıp elenir. Araç kaç koşunun tam
kurulabildiğini ve kadro doluluk dağılımını yazar.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from app.config import settings
from app.providers.nalsesleri import NalsesleriClient, parse_history, reconstruct_races


async def main(names_file: str, output: str, min_field: int = 5) -> int:
    names = [
        line.strip()
        for line in Path(names_file).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not names:
        print("at adı listesi boş")
        return 2

    print(f"→ {len(names)} at için geçmiş çekiliyor (önbellek: {settings.history_cache_dir})")
    client = NalsesleriClient(
        base_url=settings.history_api_url,
        cache_dir=settings.history_cache_dir,
        ttl_hours=settings.history_ttl_hours,
    )

    raw: dict[str, list] = {}
    failed = 0
    try:
        for index, name in enumerate(names, 1):
            try:
                runs = await client.history(name)
            except Exception as exc:
                failed += 1
                print(f"  ✕ {name}: {type(exc).__name__}: {exc}")
                continue
            raw[name] = [_run_to_row(r) for r in runs]
            if index % 25 == 0:
                print(f"  … {index}/{len(names)}")
    finally:
        await client.aclose()

    histories = {name: parse_history({"status": "success", "data": rows}) for name, rows in raw.items()}
    total_rows = sum(len(v) for v in histories.values())
    print(f"✓ {len(histories)} at · {total_rows} geçmiş satır · {failed} hata")

    # Kadro doluluğu: her koşu anahtarında kaç at var?
    sizes = Counter()
    for runs in histories.values():
        keys = {r.race_key for r in runs}
        for key in keys:
            sizes[key] += 1
    distribution = Counter(sizes.values())
    print("  kadro dağılımı (kaç atlı koşu → adet):",
          ", ".join(f"{k}→{v}" for k, v in sorted(distribution.items())))

    races = reconstruct_races(histories, min_field_size=min_field)
    print(f"✓ {len(races)} koşu yeniden kuruldu (en az {min_field} at)")
    if not races:
        print("  ipucu: daha fazla at adı ekleyin ya da --min-field değerini düşürün")

    Path(output).write_text(json.dumps({"horses": raw}, ensure_ascii=False), encoding="utf-8")
    print(f"✓ veri seti yazıldı: {output}")
    return 0


def _run_to_row(run) -> dict:
    """PastRun -> API satırı (veri setini API biçiminde saklarız)."""
    return {
        "race_date": run.race_date.isoformat(),
        "hippodrome_id": run.hippodrome_id,
        "race_number": run.race_number,
        "pist_type": run.surface,
        "distance": run.distance,
        "group_name": run.group_name,
        "horse_number": run.horse_number,
        "lane": run.lane,
        "horse_name": run.horse_name,
        "jockey": run.jockey,
        "apprentice": int(run.apprentice),
        "weight": run.weight,
        "non_runner": int(run.non_runner),
        "odds": run.odds,
        "AGF1": run.agf1,
        "AGF2": run.agf2,
        "handicap": run.handicap,
        "result": run.result,
        "time": run.seconds,
        "prize": run.prize,
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    min_field = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    raise SystemExit(asyncio.run(main(sys.argv[1], sys.argv[2], min_field)))
