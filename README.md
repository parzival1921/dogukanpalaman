# polybot — Polymarket Kazanç Sistemi

Polymarket için kurumsal kalitede, modüler bir kantitatif alım-satım botu.
Edge'i **manipülasyonda değil, daha iyi bilgi ve hızda** arar: sürekli kâr
eden cüzdanları aynala, yapısal arbitrajı milisaniyede yakala, spread'i akıllı
yönet — hepsi sert bir risk motorunun arkasında.

> **Önce paper (dry-run) mod.** Varsayılan olarak gerçek emir **gönderilmez**.
> Sistemin nasıl düşündüğünü izleyip güvendikten sonra `--live` ile aç.

---

## Ne yapar

| Strateji | Fikir | Risk |
|----------|-------|------|
| **Smart money** | On-chain'de sürekli kazanan cüzdanları tespit edip alımlarını saniyeler içinde kopyalar | Liderin yanılma riski — Kelly ile küçük sızılır |
| **Arbitraj** | YES+NO ya da birbirini dışlayan sonuçların toplamı < \$1 olduğunda her bacağı alıp garanti \$1 kazanır | Execution anında **risksiz** (kilitli) |
| **Market making** | Spread'den kazanır, envanteri quote'ları eğerek dengeler | Envanter riski — sert limitlerle sınırlı |

Her strateji yalnızca **sinyal** üretir. Hiçbiri borsaya veya portföye doğrudan
dokunamaz — her emir **risk motorundan** geçer (Kelly boyutlama + sert limitler +
günlük zarar devre kesici), sonra executor'a gider. Bu sayede risk kontrolü tek
bir yerde, denetlenebilir ve test edilebilir.

## Mimari

```
Gamma API ─┐                        ┌─ smart_money ─┐
CLOB API ──┼─► market universe ─────┼─ arbitrage ───┼─► Signal
Data API ──┘                        └─ market_making┘      │
                                                           ▼
                                               RiskEngine (Kelly + limitler + devre kesici)
                                                           │ (onay + boyut)
                                                           ▼
                                               Executor (paper | live)  ─► Portfolio (PnL, state)
```

Kaynak ağacı:
```
src/polybot/
  config.py            # YAML + env (sırlar yalnızca env'den)
  core/   models.py risk.py portfolio.py
  clients/ gamma.py clob.py data_api.py
  strategies/ base.py smart_money.py arbitrage.py market_making.py
  engine/ executor.py scheduler.py
scripts/run.py         # CLI
tests/test_core.py     # risk + matematik birim testleri (ağsız)
```

## Kurulum

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt           # paper mod + taramalar için yeterli
pip install py-clob-client                 # sadece canlı işlem (emir imzalama) için
```

## Kullanım

```bash
# 1) Hangi cüzdanlar gerçekten kazanıyor? (salt-okunur keşif)
python -m scripts.run --scan-wallets

# 2) Şu an bir arbitraj fırsatı var mı? (işlem yok)
python -m scripts.run --scan-arb

# 3) Paper modda çalıştır — gerçek para yok, sistem akıl yürütmesini izle
python -m scripts.run --config config.yaml

# 4) Canlıya geç — gerçek emir! (POLY_PRIVATE_KEY şart)
cp .env.example .env    # doldur
python -m scripts.run --live
```

## Güvenlik ve sırlar

- Özel anahtar ve API kimlikleri **yalnızca ortam değişkeninden** okunur, asla
  log'lanmaz veya dosyaya yazılmaz. `.env` ve `.polybot_state.json` git'e
  girmez (`.gitignore`).
- Canlı mod `POLY_PRIVATE_KEY` olmadan **başlamayı reddeder**.
- Portföy durumu diske yazılır; yeniden başlatınca açık pozisyonlar korunur.

## Risk ayarları (`config.yaml`)

| Parametre | Varsayılan | Anlamı |
|-----------|-----------|--------|
| `max_position_pct` | 0.05 | Tek markette en fazla bankroll'un %5'i |
| `max_total_exposure_pct` | 0.60 | Aynı anda en fazla %60 açık |
| `kelly_fraction` | 0.25 | Çeyrek-Kelly — kötü serileri atlatır |
| `max_daily_loss_pct` | 0.10 | %10 günlük düşüşte tüm işlemi durdurur |

## Testler

```bash
pip install pytest && pytest -q
```

## Yasal / etik sınır

Bu sistem **legal** stratejiler uygular. Wash trading, spoofing, pump-and-dump
gibi piyasa manipülasyonu **bilinçli olarak kapsam dışı**: bunlar hem suç (CFTC /
dolandırıcılık) hem de küçük oyuncuya para kaybettirir. Gerçek edge, başkalarını
kandırmak değil, herkesten önce doğru bilgiye ulaşmaktır.

Tahmin piyasalarında işlem bölgene göre kısıtlı olabilir ve sermaye kaybı
riski gerçektir. Kaybetmeyi göze alamayacağın parayla işlem yapma.
