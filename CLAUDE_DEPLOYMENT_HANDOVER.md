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

## 5. 2026-08-12 Oturumu: TheSports çöktü → Flashscore mimarisi + iMac göçü

**NOT (§1'i güncelleyen bilgi):** Veri kaynağı zinciri artık RapidAPI → TheSports → **Flashscore (yerel istemci)**. `v4_api_bot.py`/`thesports_bot.py` supervisor'da hâlâ dursun/durmasın tartışmasız kalabilir (zararsız, `ts_` önekiyle ayrı kayıt açıyor) ama **birincil canlı veri kaynağı artık Flashscore**.

### 5.1 Kök neden: TheSports "yetkisiz" hatası
`THESPORTS_USER`/`THESPORTS_SECRET` ile yapılan `/v1/football/match/detail_live` istekleri `{'err': 'URL is not authorized to access, please contact our business staff.'}` dönmeye başladı (muhtemelen abonelik/plan sorunu, kod tarafında hiçbir şey değişmedi). Railway loglarında görülebilir. Bu, `orchestrator`'ın veri girdisini tamamen kesti.

### 5.2 Çözüm: Flashscore tabanlı yerel-istemci mimarisi
- **`flashscore_xg_bot.py`**: Saf scraping yardımcıları (DOĞRUDAN ÇALIŞTIRILMAZ). `get_live_summary(page)` → TEK sayfa yüklemesiyle TÜM canlı maçların lig/takım/skor/dakika özetini çeker (selector'lar Playwright ile canlı sitede doğrulandı, tahmin değil). `_scrape_stats(page, mid)` → bir maçın istatistik sayfasını ziyaret edip GÖRÜNEN kategorileri (`_STAT_CATEGORY_MAP`) döner — Flashscore lige göre değişken sayıda kategori gösteriyor (bazı maçlarda sadece 5 kategori, korner/kart/atak hiç yok); görülmeyen alan için 0 UYDURULMUYOR.
- **`flashscore_xg_client.py`**: YEREL makinede çalışır (artık iMac, `launchd` ile — bkz. §5.4). Her döngüde: (1) tüm canlı maç özetini `/api/admin/live-sync`'e gönderir, (2) sunucunun kabul ettiği (bkz. aşağı) maçlardan `BATCH_SIZE=6` tanesinin detaylı istatistiğini çekip `/api/admin/live-stats-update`'e gönderir.
- **`api.py` yeni uçlar**:
  - `POST /api/admin/live-sync`: `thesports_bot.process_matches` ile AYNI mantık (matches upsert + `is_known_match` filtresi + üç-aşamalı "database is locked" koruması + stale-kapama). Flashscore GERÇEK dakikayı doğrudan verdiği için elapsed-time formülüne gerek yok. **Önemli tasarım kararı**: `orchestrator.py`'nin momentum botları (`minute-5/10/15` sorguları, bkz. `app/core/orchestrator.py:184-198`) GERÇEK zaman-serisi satırlarına ihtiyaç duyuyor — bu yüzden her senkron turunda YENİ bir `live_snapshots` satırı açılıyor, ama istatistik kolonları bir önceki satırdan **carry-forward** ediliyor (kopyalanıyor), sparse yazım diğer botları aç bırakmasın diye. Yanıtta `kabul_edilen` (fs_id listesi) dönüyor — istemci detaylı istatistik taramasını SADECE bu maçlara yapıyor (aksi halde filtrelenmiş/obscure maçlara batch slotu boşa gidiyordu).
  - `POST /api/admin/live-stats-update`: `fs_id` + `stats` dict alır, SADECE gelen alanları UPDATE eder (mevcut son `live_snapshots` satırının).
  - Eski `/api/admin/xg-targets` ve `/api/admin/xg-update` (sadece xG içindi) bu ikisiyle DEĞİŞTİRİLDİ, kaldırıldı.
- **Eski önemli hata (tespit edilip düzeltildi)**: `app/core/features.py`'de `isabet_orani = sot_toplam / sut_toplam` sadece `sut_toplam`'ı (paydayı) `if` ile kontrol ediyordu, `sot_toplam` (payı) None olabileceği kontrol edilmiyordu. Flashscore bazı maçlarda "Total shots" verip "Shots on target" vermediği için (yukarıdaki kategori değişkenliği) bu None/int çarpışması **orchestrator'ı HER TURDA çökertiyordu** (`unsupported operand type(s) for /: 'NoneType' and 'int'`) — deploy sonrası ~20 dakika HİÇ sinyal üretilmedi, Railway loglarında görülünce bulundu. Düzeltme: her iki taraf da `is not None` kontrolünden geçmeden bölme yapılmıyor.

### 5.3 `match_filter.py`: Romanya deneyi
Kullanıcı isteğiyle (`"romanya maçlarını çekelim, deneyelim"`) `is_known_match()`'e lig adında `"romania"` geçen maçları (örn. "Romanian Cup - Qualification") kabul eden bir istisna eklendi. **ÖLÇÜLMEDİ** — CLAUDE.md'nin "korelasyon 0.25 altıysa yazma" kuralı burada resmi olarak uygulanmadı, bilinçli bir deney olarak bırakıldı. Bu takımların çoğu `prematch.py`'nin arşiv-tabanlı profillerinde YOK, botlar sık sık `insufficient_data` dönebilir — bu beklenen, hata değil.

### 5.4 index.html: Sekme geçişi bug'ı
`matchListContainer` TÜM sekmeler (today/open/results/stats/login/paywall) tarafından paylaşılan TEK bir div; `fetchData()` async olduğu için `switchTab()` container'ı hemen sıfırlamıyordu — bir önceki sekmenin içeriği (örn. "Üye Ol" login formu) yeni sekmenin verisi gelene kadar ekranda kalıyordu. Kullanıcı "sayfalar arası geçişte hata" olarak bildirdi, Playwright ile canlı sitede tekrar üretildi (login → "Açık Tahminler" geçişinde login formu görünüyordu). Düzeltme: `switchTab()` artık today/open/results'a geçerken container'ı hemen yükleniyor-spinner'ına sıfırlıyor.

### 5.5 iMac göçü (laptop elden çıkıyor)
- Bu laptop kullanımdan kalkacağı için proje **iMac'e taşındı** (`haktanerdogan@192.168.1.195`, ev ağında `Haktan-iMac`). iMac'te 4 Ağustos'tan kalma ESKİ bir klon zaten vardı (muhtemelen iOS/Capacitor işi için) — `git pull` ile güncellendi (184 commit ileri), bekleyen önemsiz bir yerel değişiklik (geçici `DEMO_MODE` togglesı, zaten upstream'de kaldırılmıştı) stash ile güvenle temizlendi.
- **`flashscore_xg_client.py` artık iMac'te `launchd` ile SÜREKLİ/OTOMATİK çalışıyor** — `~/Library/LaunchAgents/com.matchrix.flashscoreclient.plist` (`RunAtLoad` + `KeepAlive` true, log'lar `/tmp/flashscore_client.log` / `_err.log`). Bu, canlı veri akışının TEK kaynağı — bu servis dururursa sinyal üretimi tamamen durur (TheSports zaten çalışmıyor).
- `iddaa` projesi (ayrı, git'siz, `/Users/sebnem/Desktop/iddaa` → iMac'te `~/Desktop/iddaa`) `rsync` ile taşındı, ayrı bir `.venv` kuruldu (pandas/numpy/requests/bs4/cloudscraper/openpyxl).
- **Devralacak AI için önemli**: iMac'te bir şey bozulursa önce `launchctl list | grep matchrix` ve `tail -f /tmp/flashscore_client.log` ile bak. `BACKUP_SECRET` plist içine EnvironmentVariables olarak gömülü (Railway Variables'daki değerle aynı olmalı).

### 5.6 Şu Anki Durum (2026-08-12 sonu itibarıyla)
- Railway'de canlı, deploy'lar sorunsuz. Orchestrator çökmeden çalışıyor, sinyal üretiyor (Romanya dahil).
- Bilinen sınırlama: xG SADECE Flashscore'un üst düzey ligler için gösterdiği bir kategori — küçük/bölgesel liglerde (Romanya dahil) hiç yok, `bot_xg_sniper`/`bot_finishing_gap` bu maçlarda oy veremiyor (kod hatası değil, veri gerçekten yok).
- `thesports_bot.py` supervisor'da hâlâ çalışıyor (yetkisiz hatası vererek, zararsız) — TheSports hesabı düzelirse `ts_` önekli paralel kayıtlar açabilir, aynı gerçek maç için Flashscore (`fs_`) ile ÇİFT sinyal riski teorik olarak var ama şu an düşük öncelikli.
