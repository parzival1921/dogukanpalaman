# At Yarışı Tahmin Dashboard

Koşu programını bir API'den çeker, her at için kazanma olasılığı üretir, **en kârlı
(en yüksek beklenen değerli)** bahisleri seçer, koşunun başlamasına **5 dakika kala
tahmini kilitler** ve koşu bitince **kazanıp kazanmadığını ve kazanılan tutarı** yazar.

```
program çek ──► oranları izle ──► T-5dk: KİLİTLE ──► koşu ──► sonuç + ödeme ──► kasa
                    (her 20 sn)     (değiştirilemez)          (kazandı/kaybetti, ₺)
```

## Hızlı başlangıç

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Demo (simülasyon) modu — API anahtarı gerekmez, döngüyü canlı izlersiniz
uvicorn app.main:app --reload --port 8000
```

Tarayıcıdan <http://localhost:8000> açın. Varsayılan demo ayarlarında ilk koşu
uygulamayı açtıktan ~6 dk sonra başlar; 1 dk kala kart "KİLİTLİ" olur ve koşu
bitince sonuç tablosuna düşer. Daha hızlı görmek için:

```bash
DEMO_FIRST_POST_MINUTES=1 DEMO_RACE_INTERVAL_MINUTES=0.6 SETTLE_DELAY_MINUTES=0.2 \
SYNC_INTERVAL_SECONDS=5 uvicorn app.main:app --port 8000
```

Testler ve geriye dönük test:

```bash
pytest -q                        # 40 test: model, bahis seçimi, kilit, sonuçlandırma, API
python -m tools.backtest 250 6   # 1500 koşuluk simülasyon: ROI, isabet, kalibrasyon
```

`tools.backtest` canlı akışın aynısını hızlandırılmış çalıştırır ve **Brier skoru**
ile modeli piyasayla karşılaştırır (düşük olan daha iyi tahmin ediyor demektir).
Varsayılan ayarlarla demo simülatöründe tipik çıktı:

```
bahis   : 905  (koşu başına 0.60)
isabet  : %17.3
ROI     : %+41.1
Brier model/piyasa : 0.0954 / 0.0965
```

> Bu ROI **simülasyona aittir, gerçek bir getiri vaadi değildir.** Demo pazarı
> kasten yenilebilir kurgulanmıştır: simülasyondaki "halk" son forma ve jokeye
> fazla, mesafe/pist uyumu ile dinlenme süresine ise neredeyse hiç ağırlık verir
> (`DemoProvider.PUBLIC_WEIGHTS`), gerçek sonuç ise bunların hepsine bağlıdır.
> Gerçek pari-mutuel pazarda komisyon %15-20'dir ve bu tür bir avantaj çok daha
> zor bulunur — bu yüzden aynı testi kendi veri kaynağınızla tekrar çalıştırın.

## Gerçek veriyi bağlamak

> **Önemli:** TJK'nın herkese açık ve dokümante edilmiş bir REST API'si yok. Bu depodaki
> `config/provider.tjk.json` gözlemlenen `ajax.tjk.org` uçlarına göre yazılmış bir
> **taslaktır ve olduğu gibi çalışmaz** — geliştirme ortamında tjk.org ağ politikası
> nedeniyle erişilemediği için doğrulanamadı. Alan adlarını kendi gördüğünüz JSON
> yanıtına göre düzeltmeniz gerekir.

Veri katmanı eklentili (pluggable): kod yazmadan, bir **eşleme (mapping) dosyasıyla**
herhangi bir JSON API'ye bağlanırsınız.

```bash
# 1) Tarayıcıda DevTools > Network ile isteği yakalayın, JSON yanıtını dosyaya kaydedin
#    (Response > sağ tık > Save / Copy)

# 2) Eşlemeyi ham JSON'dan otomatik çıkarın — Türkçe alan adlarını tanır
python -m tools.infer_mapping program.json > mapping.json
python -m tools.infer_mapping ganyan.json --section odds
python -m tools.infer_mapping sonuc.json  --section results

# 3) Çıkan eşlemeyi config/provider.<ad>.json içine koyun; "path", "params" ve
#    "base_url" alanlarını gerçek istekten doldurun (bunlar tahmin edilemez)

# 4) Ağa çıkmadan doğrulayın:
python -m tools.check_provider config/provider.<ad>.json --sample program.json \
    --sample-odds ganyan.json --sample-results sonuc.json

# 5) Canlıya alın:
PROVIDER=http PROVIDER_CONFIG=config/provider.<ad>.json uvicorn app.main:app --port 8000
```

`infer_mapping` JSON içindeki kayıt listelerini bulur, anahtar adlarını Türkçe/İngilizce
eş anlamlı sözlüğüyle eşler ve örnek değerlerden tipi çıkarır — ondalık virgül
(`"58,5"` → `58.5`), form dizisi (`"1-3-2-1-4"` → `[1,3,2,1,4]`) ve tarih biçimi
(`"21/08/2026 14:30"`) dahil. Tahmin kesin değildir; çıktıyı gözden geçirin.

`check_provider` her adımda kaç kayıt çözümlendiğini ve hangi alanın boş geldiğini
yazar — `--sample` ile kaydedilmiş dosyadan, argümansız ise canlı API'den.
Şema ve tüm alan seçenekleri: `config/provider.example.json`.

Kendi sağlayıcınızı Python ile yazmak isterseniz `app/providers/base.py` içindeki üç
metodu (`fetch_race_cards`, `fetch_odds`, `fetch_result`) doldurup
`app/providers/__init__.py` içinde kaydedin.

## Tahmin nasıl üretiliyor?

`app/engine/` altında, üç adım:

**1. Özellikler** (`features.py`) — her at için koşu içinde göreli olarak yorumlanan
sinyaller: recency-ağırlıklı son 5 koşu formu, handikap puanı, jokey kazanma oranı,
kilo avantajı, mesafe ve pist uyumu, dinlenme süresi eğrisi, start yeri (kısa kum
koşularında ağırlığı artar), yaş uyumu.

**2. Olasılık** (`predictor.py`) — özellikler koşu içi z-skora çevrilir, ağırlıklı
toplanır, **bileşik skor yeniden standartlaştırılır** (aksi halde softmax'ın
keskinliği ağırlık toplamına ve at sayısına göre kayar ve olasılıklar aşırı sivri
çıkar) ve softmax ile olasılığa dönüşür. `SOFTMAX_TEMPERATURE` böylece anlamlı bir
kalibrasyon parametresi olur; 1.1 civarı favoriye ~%25-30 verir. Ganyan oranlarından **overround temizlenerek**
piyasa olasılığı çıkarılır ve ikisi harmanlanır (`MARKET_BLEND`, varsayılan 0.40).

Sonra bir **emniyet freni** çalışır: modelin bir ata biçtiği olasılık, piyasanınkinin
`MAX_PROB_RATIO` (varsayılan 3) katını aşamaz. Sezgisel bir model tek bir bozuk
özellik yüzünden saçma bir avantaj görebilir; bu fren, model ile piyasa arasındaki
logaritmik karışımı ikili aramayla küçülterek bunu engeller.

**3. Bahis seçimi** (`betting.py`) — "en kârlı" = en yüksek olasılık değil, en yüksek
**beklenen değer**:

```
EV = p × oran − 1          (1 TL'lik bahis başına beklenen kâr)
Kelly = (p × b − q) / b,   b = oran − 1
stake = bankroll × min(Kelly × KELLY_FRACTION, MAX_STAKE_PCT)
```

Filtreler: `MIN_EV` altındaki avantajlar oynanmaz; `MAX_EV` üstündeki "fazla iyi"
EV'ler ise gerçek avantaj değil **veri hatası** kabul edilip elenir (bayat veya yanlış
ayrıştırılmış oran). Koşu başına en fazla `MAX_BETS_PER_RACE` bahis ve toplam risk
tavanı uygulanır.

## Kilitleme (T-5 dk)

Arka plan döngüsü (`scheduler.py`) her `SYNC_INTERVAL_SECONDS` saniyede bir çalışır.
Start'a kalan süre `LOCK_MINUTES` (varsayılan 5) dakikanın altına düştüğünde koşunun
tahmini **o anki oranlarla** dondurulur:

* tüm atların değerlendirmesi, seçilen bahisler, oranlar ve kasa anlık görüntüsü
  `predictions` / `bets` tablolarına yazılır,
* stake aynı anda kasadan düşülür,
* kayıt **değiştirilemez**: SQLite tetikleyicileri kilitten sonra tahmin ve bahis
  satırlarının değişmesini reddeder (yalnızca sonuçlandırma alanları yazılabilir).

Yani kilitten sonra oran hareket etse de tahmin ve stake asla geriye dönük
"düzeltilmez" — sonuçlar dürüsttür. (`tests/test_flow.py` bunu doğrular.)

## Sonuçlandırma

Koşu bitip sonuç geldiğinde (`SETTLE_DELAY_MINUTES` sonra sorgulanır) her bahis
kapatılır:

| Durum | Koşul | Ödeme |
|---|---|---|
| **KAZANDI** | at 1. (plase bahsinde ilk 2/3) | resmi temettü × stake, yoksa kilit anındaki oran |
| **KAYBETTİ** | derece tutmadı | 0 (kayıp = stake) |
| **İADE** | koşu iptal / at koşmadı | stake geri |

Plase kuralı TJK'daki gibi: 8+ atlı koşuda ilk 3, 5-7 atlıda ilk 2.
Kasa hareketleri `ledger` tablosunda tutulur; dashboard'daki kasa eğrisi bu tablodan gelir.

## Dashboard

* **KPI**: kasa, toplam kâr, ROI, isabet oranı, bekleyen bahis
* **Kasa eğrisi**: her kilit/ödeme sonrası bakiye (hover ile detay + veri tablosu)
* **Yaklaşan koşular**: geri sayım (kilide ve start'a), CANLI/KİLİTLİ rozeti, ilk 5 at
  için model/piyasa olasılığı ve EV, seçilen bahisler ve stake'ler
* **Sonuçlanan tahminler**: tahmin, oran, stake, ✓ KAZANDI / ✕ KAYBETTİ / — İADE ve
  kazanılan tutar, koşunun ilk 3'ü

## API

| Uç | Açıklama |
|---|---|
| `GET /api/dashboard` | Dashboard'un tüm verisi (5 sn'de bir çekilir) |
| `GET /api/races` · `GET /api/races/{id}` | Yaklaşan koşular · koşu detayı + tahmin |
| `GET /api/bets?status=won\|lost\|pending` | Bahis listesi |
| `GET /api/results` | Sonuçlanan tahminler |
| `GET /api/ledger` | Kasa hareketleri |
| `POST /api/sync` | Döngüyü elle tetikle |
| `GET /api/health` | Sağlayıcı, son tick, son hata |

## Ayarlar (ortam değişkenleri)

| Değişken | Varsayılan | Ne yapar |
|---|---|---|
| `PROVIDER` | `demo` | `demo` veya `http` |
| `PROVIDER_CONFIG` | `config/provider.tjk.json` | Eşleme dosyası |
| `DATABASE_PATH` | `data/atyarisi.db` | SQLite dosyası |
| `LOCK_MINUTES` | `5` | Kaç dk kala kilitlenir |
| `SYNC_INTERVAL_SECONDS` | `20` | Döngü sıklığı |
| `SETTLE_DELAY_MINUTES` | `2` | Start'tan kaç dk sonra sonuç sorulur |
| `BANKROLL_START` | `1000` | Başlangıç kasası (TL) |
| `MARKET_BLEND` | `0.40` | Piyasa olasılığının harmandaki ağırlığı |
| `MAX_PROB_RATIO` | `3.0` | Modelin piyasadan azami sapması |
| `MIN_EV` / `MAX_EV` | `0.08` / `1.00` | Oynanacak EV aralığı |
| `KELLY_FRACTION` | `0.25` | Kelly'nin kaçta kaçı oynanır |
| `MAX_STAKE_PCT` | `0.04` | Tek bahiste kasanın azami yüzdesi |
| `MAX_BETS_PER_RACE` | `2` | Koşu başına azami bahis |
| `MIN_ODDS` / `MAX_ODDS` | `1.40` / `30` | Oynanabilir oran aralığı |
| `SOFTMAX_TEMPERATURE` | `1.1` | Olasılık dağılımının keskinliği (küçük = sivri) |
| `TIMEZONE` | `Europe/Istanbul` | Arayüzdeki saat dilimi |
| `DEMO_MARKET_BIAS` / `DEMO_OVERROUND` | `0.22` / `1.18` | Demo pazarının yanılgısı ve komisyonu |

## Proje yapısı

```
app/
  main.py           FastAPI uygulaması + JSON API
  scheduler.py      arka plan döngüsü (senkron, kilit, sonuç)
  service.py        iş akışı: kilitleme, sonuçlandırma, dashboard verisi
  db.py             SQLite şeması + değiştirilemezlik tetikleyicileri
  models.py         RaceCard / Runner / RaceResult / BetProposal
  engine/           features · predictor · betting · settle
  providers/        base · demo (simülatör) · http_json (eşlemeli adaptör)
  static/           dashboard (HTML + CSS + vanilla JS)
config/             sağlayıcı eşleme dosyaları
tools/              infer_mapping (eşleme çıkarımı) · check_provider (doğrulama)
                    backtest (strateji testi)
tests/              40 test
```

## Sorumluluk reddi

Bu araç analiz ve simülasyon amaçlıdır; yatırım veya bahis tavsiyesi değildir.
Kâr/zarar rakamları varsayımsal bir kasa üzerinden hesaplanır. Türkiye'de yasal
bahis yalnızca TJK/Bilyoner gibi lisanslı kanallardan oynanır ve bu uygulama hiçbir
bahis kanalına otomatik olarak bahis göndermez.
