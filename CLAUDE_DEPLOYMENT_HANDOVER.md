# JCODE ANALYTICS | DEPLOYMENT & BUGFIX HANDOVER (Claude)

Bu doküman, `CLAUDE_HANDOVER.md`'de tanımlanan sistem üzerinde Claude'un (Cowork) yaptığı deployment ve hata düzeltme çalışmalarını özetler. Sistemi devralacak başka bir AI asistanı (ör. Antigravity) için hazırlanmıştır.

## 1. Hosting / Deployment

- **Platform:** Railway (Vercel önce denendi, uygun olmadığı için terk edildi).
- **Kaynak:** GitHub reposu, Railway'in GitHub App'i üzerinden bağlı — `git push` yapıldığında otomatik deploy tetikleniyor.
- **Süreç yönetimi:** Tek bir Railway servisi, `Procfile` ile `python3 supervisor.py` çalıştırıyor. `supervisor.py` üç alt süreci ayakta tutuyor:
  - `api` → `uvicorn api:app --host 0.0.0.0 --port $PORT`
  - `v4_api_bot` → RapidAPI'den canlı veri çekici
  - `orchestrator` → `app/core/orchestrator.py` (18 bot + konsensüs)
  - (`live_bot.py` / sofascore ve `sporkolik_bot.py` scraper'ları devre dışı bırakıldı; **tek gerçek veri kaynağı RapidAPI**.)
- **Loglar:** `supervisor.py` alt süreçlerin stdout'unu hem yerel log dosyasına hem de ana process'in stdout'una (Railway log paneline) basacak şekilde thread'li bir `_pump_output` fonksiyonuyla yeniden yazıldı — başta sadece `[supervisor]` satırları görünüyordu, alt süreçlerin çıktısı Railway'e hiç ulaşmıyordu.
- **Ortam değişkenleri:** `RAPIDAPI_KEY` Railway Variables üzerinden set edildi; kod tarafında hardcoded key kaldırıldı, `RAPIDAPI_KEY` env değişkeni yoksa `RuntimeError` fırlatılıyor (GitHub'a güvenlik açığı push edilmesin diye).
- **Veritabanı:** `database/fh_goal_predictor.db` repoya commit edildi (`.gitignore` sadece `__pycache__`, `logs/`, `*.zip`, `.db-journal/.wal/.shm` gibi geçici dosyaları hariç tutuyor) — böylece ilk Railway deploy'unda gerçek/geçmiş veriyle başlıyor.

## 2. Bulunan ve Düzeltilen Hatalar

### 2.1 Orkestratör çöküyordu
`app/bots/bot_dark_form.py` (bot 18/DarkFormBot) yanlış alan adları kullanıyordu (`match_id_db`, `confidence` yerine `confidence_level`, `reasons` listesi yerine `reason` string'i, `data_quality` float yerine `"Yüksek"` string'i) — orkestratör her döngüde bu bot yüzünden exception atıyordu. Alan adları düzeltildi.

### 2.2 Frontend Railway'de veri çekemiyordu
`index.html` içindeki `fetchData()` ve `openMatchDetails()` fonksiyonları `http://localhost:8000` hardcoded prefix kullanıyordu. Relative URL'e çevrildi (`/api/...`). Bu düzeltilmeden Railway'deki site "Sunucuya bağlanılamadı" hatası veriyordu.

### 2.3 İlk yarı / maç sonu market karışıklığı + bitmiş maç dakikası
- `matches.status` (`LIVE`/`HT`/`FINISHED`) frontend'e hiç gönderilmiyordu; bitmiş maçlarda hâlâ "94. dakika" gibi canlı dakika gösteriliyordu. `/api/live-matches` cevabına `match_status` eklendi, `index.html`'de "MS" (maç sonu) / "İY" (devre arası) / canlı dakika ayrımı yapıldı.
- İlk yarı marketleri (`İlk Yarı X.5 Üst`) yanlışlıkla 2. yarı gollerine göre de sonuçlanıyordu (ör. Ajax "5.5 Üst" maçı 4.5 Üst bittiği halde kayıp olarak işlenmemişti). `live_snapshots` üzerinden ilk yarı sonu skorunu (`fh_end_home/away`) hesaplayan bir subquery eklendi; ilk yarı marketleri SADECE bu skora göre, maç sonu marketleri güncel skora göre sonuçlanıyor.

### 2.4 Aynı maç birden fazla kez / kazanan-kaybeden satırları kayboluyordu
Orkestratör bir maça zaman içinde birden fazla sinyal üretebiliyor (goller ilerledikçe eşik yükselip yeni sinyal açılıyor) — `consensus_predictions` tablosunda aynı maç için birden fazla satır oluşuyordu ve `/api/live-matches` bunların hepsini ayrı satır olarak dönüyordu (ekranda 2-3 kez aynı maç). `api.py` yeniden yazıldı: sonuçlar `match_id`'ye göre gruplanıp WON > PENDING > LOST önceliğiyle **her maç için tek satır** dönecek şekilde birleştirildi.

### 2.5 Maçlar sonsuza kadar canlı/açık görünüyordu
- **Kota tükenmesi (kullanıcı teşhisi, doğrulandı):** Bazı maçlar 90+ dakikada donmuş görünüyordu — kullanıcı bunun ücretsiz/kalitesiz veri değil, RapidAPI sorgu kotasının dolması olduğunu belirtti; plan yükseltilince kendiliğinden düzeldi. (Not: Claude başta yanlışlıkla "ücretsiz API veri kalitesi sorunu" sanıp bir "stale-minute" heuristiği eklemişti, kullanıcının düzeltmesiyle bu heuristik tamamen geri alındı — böyle bir workaround YOK, doğrudan RapidAPI planına bağlı.)
- **Sonsuz "HT" bugu (asıl kök neden, bugün düzeltildi):** `v4_api_bot.py`'deki temizlik sorgusu, canlı feed'den tamamen düşen (bir daha hiç dönmeyen) maçları, dakikaları 40-50 aralığındaysa `'HT'` yapıyordu — sonsuza kadar. `Maç Sonu` marketleri sonucu sadece `'FINISHED'` durumunda hesapladığından, böyle bir maç aylarca/yıllarca "Açık Bahisler"de PENDING kalıyordu (kullanıcının bildirdiği "Kiev maçı bir yıldır açık" hatası buydu). Düzeltme: feed'den düşen maç artık koşulsuz `'FINISHED'` yapılıyor.

### 2.6 Sinyal dakikası / market etiketi sabitlenmedi
`consensus_predictions` tablosunda sinyalin hangi dakikada ve hangi market için üretildiği ayrıca saklanmıyordu; `api.py` bunu her seferinde maçın **o anki** (değişen) dakikasından yeniden hesaplıyordu. Bu yüzden ilk yarıda üretilen bir sinyal, maç 2. yarıya geçtiğinde "Maç Sonu" marketine dönüşebiliyordu. Çözüm: `consensus_predictions` tablosuna `signal_minute` ve `market` kolonları eklendi (otomatik migration), orkestratör sinyali üretirken bu değerleri SABİT olarak yazıyor, `api.py` bunları doğrudan okuyor (eski satırlar için eski mantığa fallback var). `index.html`'de "Açık Bahisler" kartlarına "Tahmin Dakikası: X'" satırı eklendi.

## 3. Şu Anki Durum

- Railway üzerinde canlı, `git push` ile otomatik deploy oluyor.
- Tüm düzeltmeler yukarıdaki sırayla commit edilip push edildi (kullanıcı kendi terminalinden push ediyor).
- Bilinen açık konu yok; en son düzeltme (2.5 ve 2.6) push bekliyor / yeni push edildi.

## 4. Devralacak AI için notlar

- Git push/commit her zaman kullanıcı tarafından kendi terminalinden yapılıyor (Claude'un çalıştığı ortamda GitHub kimlik bilgisi ve dosya silme/unlink yetkisi yok).
- Değişiklik yapmadan önce local'de (repo kopyası üzerinde) test edip, `python3 -m py_compile` ile syntax kontrolü yapmak faydalı oldu.
- RapidAPI kotası sınırlı olabilir; "maç donmuş görünüyor" tarzı raporlarda önce kota/plan durumuna bakmak, veri kalitesi varsayımı yapmamak gerekiyor.
