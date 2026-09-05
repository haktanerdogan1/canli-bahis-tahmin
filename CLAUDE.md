# JCode Analytics — Proje Kuralları

Canlı bahis gol-sinyali tahmin sistemi. Genel mimari için `CLAUDE_DEPLOYMENT_HANDOVER.md`'ye bak.

## 1. Yeni bot yazmadan önce: out-of-sample test zorunlu

Yeni bir bot fikri geldiğinde kod yazmadan ÖNCE şu ölçüm yapılır:
takımların maçları kronolojik olarak ikiye bölünür, geçmiş yarının
gelecek yarıyı tahmin edip etmediği ölçülür (korelasyon).

**Korelasyon 0.25'in altındaysa bot yazılmaz.** Bu eşik bir fikri
gerçekten eledi: "2. yarı gol eğilimi" sinyali test edildiğinde r=0.11
çıktı ve terk edildi.

## 2. Ölçmeden iddia yok

Bir botun/sinyalin başarı oranı hakkında konuşmadan önce `/api/metrics`'te
yeterli sayıda **sonuçlanmış** (outcome dolu) sinyal olduğundan emin ol.
Az örnekle ("3 sinyalin 2'si tuttu") performans iddiası yapılmaz.

## 3. Taban oranlar (arşivden ölçülmüş, referans değerler)

- İlk yarı gol: **%61.6**
- Maç sonu 1.5 üst: **%73.7**
- Maç sonu 2.5 üst: **%50.2**

Bir bot bu taban oranların altında isabet gösteriyorsa, o bot değer
katmıyor demektir — rastgele tahminden daha iyi değildir.

### 3b. Market bazlı sinyal kısıtları (ölçümle eklenir)

- **İlk Yarı gol sinyalleri SADECE dakika ≤ 25'te açılır** (kullanıcı talebi,
  2026-08-30). Ölçüm (son 2 gün, İY sinyalleri, dakika bandına göre):
  0-15. dk %72 (23G/9K) · 16-25. dk %57 (16G/12K) · **26-35. dk %36 (9G/16K)**.
  İlk yarı bitmeye yakın "bir gol daha" kovalamak sistemli kaybettiriyor.
  `orchestrator.py`: `if (25 < minute < 46) ...: continue` (36-45 zaten
  bloktaydı, alt sınır 35 → 25'e çekildi). Maç sonu sinyalleri etkilenmez.
- **"İlk Yarı 2.5 Üst"** (ilk yarıda zaten 2 gol varken 3. golü beklemek):
  ölçülen isabet %44 (8G/10K) — coin-flip'in ve diğer İY marketlerinin
  altında. `orchestrator.py`: bu market SADECE skor **1-1 VE dakika ≤ 20**
  iken açılır; 2-0/0-2 veya dk > 20 ise sinyal hiç üretilmez (kullanıcı
  talebi + gözlem, 2026-08-30). Gri bölge (dk 21-24) bloke tarafında.
- **"İlk Yarı 3.5 Üst" ve üstü** (ilk yarıda 3+ gol varken 4./5. golü beklemek):
  `total_goals_initial >= 3` iken İY sinyali **her zaman** bloke (kullanıcı
  talebi 2026-08-30). İstatistiksel temeli yok; kod bunu üretebiliyordu çünkü
  market adı `f"İlk Yarı {total_goals_initial + 0.5} Üst"` ve üst sınır yoktu.

## 4. `outcome` sütunu değiştirilemez

`consensus_predictions.outcome` bir kez yazıldıktan sonra **ASLA**
güncellenmez veya silinmez (bkz. `settlement.py` — `WHERE ... AND outcome
IS NULL` koruması bilerek var). Geçmiş sonuçlar bozulmamalı; sinyal
kalibrasyonu bu verinin bütünlüğüne dayanıyor.

## 5. Test sonrası temizlik

Test amacıyla `/tmp`'ye proje kopyası bırakılmaz. İş bitince kopya silinir.

## 6. Canlı veri kaynakları — hangisi nerede çalışıyor

Railway'deki `web` servisi `supervisor.py` ile şu süreçleri yönetir (Railway
kapanmadıkça, kullanıcının bilgisayarından BAĞIMSIZ çalışırlar):
- `api` (uvicorn), `thesports_bot` (yetkisiz hatası veriyor ama **kullanılmıyor**,
  önemsiz), `orchestrator` (18 bot konsensus), `sevenm_client` (7msport.com —
  Playwright gerekmiyor, hafif, 2026-08-24'te "PC kapansa da çalışsın" diye
  buraya taşındı), `iddaa_odds_client`.

`flashscore_xg_client.py` ve `sofascore_client.py` ise Chromium/Playwright
gerektirdiği için Railway'de ÇALIŞTIRILAMIYOR (bellek limiti — bkz.
`flashscore_xg_client.py` docstring'i, Chromium container'ı 1GB limitine
dayamıştı). Bu ikisi **SADECE kullanıcının kendi bilgisayarında**, launchd
ile çalışır:
- `~/Library/LaunchAgents/com.matchrix.flashscoreclient.plist`
- `~/Library/LaunchAgents/com.matchrix.sofascoreclient.plist`
- (ayrıca `com.matchrix.sevenmclient.plist` de var ama gereksiz — sevenm zaten
  Railway'de `supervisor.py` üzerinden çalışıyor, local kopyası kullanılmıyor)

**2026-09-03 düzeltme: "iMac artık yok" notu YANLIŞTI** — iMac geri geldi/hâlâ
var. Kullanıcının HEM MacBook Air HEM iMac'i var, flashscore/sofascore hangi
makinede çalışacağı **her gün değişebilir** (o gün hangisi kullanılıyorsa -
eşi de MacBook Air'i kullanabiliyor). Bu ikisi AYNI ANDA iki makineden
ÇALIŞTIRILMAMALI (aynı `fs_`/`ss_` source prefix'iyle çift veri girer,
`match_id` çakışması/karışıklık riski). Her Claude Code oturumu SADECE
üzerinde çalıştığı makineye erişir - bir oturum diğer makineye "izin
verilse" bile geçemez, o makinede AYRI bir Claude Code oturumu açılması
gerekir (kendi launchd/dosya erişimiyle).

Kontrol/başlatma (üzerinde bulunulan makinede):
```
launchctl list | grep matchrix
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.matchrix.flashscoreclient.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.matchrix.sofascoreclient.plist
tail -f /tmp/flashscore_client.log /tmp/sofascore_client.log
```
Durdurma (diğer makineye geçerken, çift veri girmesin diye):
```
launchctl bootout gui/$(id -u)/com.matchrix.flashscoreclient
launchctl bootout gui/$(id -u)/com.matchrix.sofascoreclient
```
`RunAtLoad`+`KeepAlive` true olduğu için bilgisayar yeniden başlayınca zaten
otomatik açılırlar — sadece ilk kurulumda/manuel durdurulduysa elle
bootstrap etmek gerekir.

## 6b. Veritabanı bakımı — 2026-09-05 tıkanması ve kalıcı korumalar

**Ne oldu:** `live_snapshots` hiç temizlenmiyordu. Tablo 13 milyon satıra,
DB 1.1 GB'a ulaştı; yazma kilidi sürekli doldu. Orkestratör AÇILIŞTA taban
oranları ve oran profillerini kuramadı ("database is locked"), bu veriler
SADECE açılışta bir kez kurulduğu için bir daha denenmedi ve botlar
"yetersiz veri" deyip çekildi: **12 saat boyunca sıfır sinyal**.
Kullanıcı bunu "web sitemiz hala 3 maçta kalmış" diye bildirdi.

**Artık geçerli olan kurallar:**

1. **Referans verisi kurulumu tek seferlik OLAMAZ.** `orchestrator.py`
   `REFERANS_ISLERI` + `_referans_bekleyen`: açılışta kurulamayan iş bakım
   turunda kurulana kadar tekrar denenir. Yeni bir "açılışta bir kez kurulan"
   şey eklerken bu listeye ekle — geçici bir kilidi kalıcı körlüğe çeviren
   desen tam olarak budur.
2. **Bakım turunda referans verisi temizlikten ÖNCE gelir.** Sinyal üretimi
   ona bağlı; snapshot temizliği 5 dakika bekleyebilir. Ters sırada 200 binlik
   silme DB'yi meşgul bırakıp referans işlerini aç bırakıyordu.
3. **Toplu silme parti SAYISIYLA büyütülür, parti BOYUTUYLA değil**
   (`drain_old_snapshots`). Tek işlemde 200 bin satır silmek kilidi onlarca
   saniye tutar ve 30sn'lik `busy_timeout`'a takılan herkesi aç bırakır —
   çözmeye çalıştığımız sorunun ta kendisi.
4. **Yazma uçlarında `try/finally: conn.close()` zorunlu.** İstisna
   `conn.close()`'u atlarsa bağlantı ancak çöp toplayıcıyla kapanır; o ana
   kadar tuttuğu okuma anlık görüntüsü WAL'ın checkpoint edilmesini engeller,
   yani her hata bir sonrakini daha olası kılar (bkz. `live_stats_update`).
5. **DB dosyası küçülmez, küçülmesi de gerekmez.** SQLite silinen sayfaları
   serbest listeye alıp yeniden kullanır. Önemli olan taranan satır sayısı.
   `VACUUM` tüm veritabanını dakikalarca kilitler — canlı sistemde çalıştırma.

**İzleme:** `log_db_health()` her bakım turunda `[settlement] DB durumu:
db=... snapshot=... temizlik_kuyrugu=...` yazar. Yavaşlık şikayetinde ÖNCE
bu seriye bak — kuyruk erimiyorsa temizlik yetişmiyordur.

**Uyarı (acı deneyim):** `/api/live-matches` sorgusunu "optimize etme"
denemesi sentetik veriyle doğrulanmasına rağmen üretimde 2.5-10.7sn'yi
**53.6sn'ye çıkardı** ve geri alındı. Bu uçta sorgu değişikliği ÜRETİMDE
ölçülmeden gönderilmez.

## 7. Bilinen açık konular

- `bot_red_card` ve `bot_attack_volume`: baktıkları veri alanı canlı
  API'den hiç dolmadığı için pratikte hiç çalışmıyor.
- xG botları: veri sadece **%13** oranında geliyor, çoğu maçta
  `insufficient_data` ile çekiliyor.
- `v4_api_bot.py` içinde bir istatistik-anahtarı keşif mekanizması var;
  Railway loglarında `"YENI ISTATISTIK ANAHTARI"` satırları, API'nin
  aslında hangi alanları doldurduğunu gösterir — yukarıdaki botları
  düzeltmenin yolu oradan geçiyor.
- **Bloke takımlar (şike riski, kullanıcı talebi):** `match_filter.py`
  `BLOCKED_TEAM_SUBSTRINGS` / `BLOCKED_TEAM_EXACT`. `is_known_match()` bunları
  lig bloklarından hemen sonra, KNOWN_TEAMS/arşiv kademelerinden ÖNCE reddeder.
  - 2026-08-30: Alianza FC, Atlético Balboa, Junior (Barranquilla),
    (Independiente) Santa Fe, Vasco da Gama, Cruzeiro (Vasco + Cruzeiro
    KNOWN_TEAMS'ten de çıkarıldı).
  - 2026-09-01: yeni şike dalgası, 7 maç (kazananlar dahil). Bilinen 2 maç:
    Estudiantes L.P.–Newell's Old Boys, Remo–Coritiba. Bu 4 takım eklendi;
    o 2 maçın sinyalleri `void-pending-signals` ile siliniyor (settled
    olanlar için `match_ids` + `allow_settled_for_named_matches=true`). Kalan
    5 maçın takım adları netleşince eklenecek.

- **K3 League (Güney Kore, izleniyor, 2026-08-29):** NPL/Capital Football
  gibi bilerek bloke EDİLMEDİ — hiçbir şike iddiası/kötü ölçüm yok, sadece
  yapısal olarak ayni risk kategorisinde (yarı-profesyonel, alt kademe).
  Kullanıcı kararı: kanıt gelmeden bloke etme, birkaç hafta gerçek
  performansını izle. İlk sinyal 2026-08-29'da (Dangjin Citizen-Chuncheon,
  o an PENDING) - örneklem büyüyünce `bot-sinyalleri`de "k3 league" filtrele,
  isabet oranı taban oranların (kural 3) belirgin altındaysa bloke listesine
  ekle (bkz. `match_filter.py` BLOCKED_LEAGUE_NAMES, NPL Victoria orneği).
