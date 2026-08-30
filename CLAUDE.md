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

- **"İlk Yarı 2.5 Üst"** (ilk yarıda zaten 2 gol varken 3. golü beklemek):
  ölçülen isabet %44 (8G/10K) — coin-flip'in ve diğer İY marketlerinin
  altında. `orchestrator.py`: bu market SADECE skor **1-1 VE dakika ≤ 20**
  iken açılır; 2-0/0-2 veya dk > 20 ise sinyal hiç üretilmez (kullanıcı
  talebi + gözlem, 2026-08-30). Gri bölge (dk 21-24) bloke tarafında.

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

**2026-08-28: iMac artık yok**, kullanıcı bu ikisini (flashscore + sofascore)
şu an bu bilgisayardan (MacBook Air) çalıştırıyor. Bilgisayar kapanırsa/uyursa
bu iki kaynak durur — kontrol/başlatma:
```
launchctl list | grep matchrix
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.matchrix.flashscoreclient.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.matchrix.sofascoreclient.plist
tail -f /tmp/flashscore_client.log /tmp/sofascore_client.log
```
`RunAtLoad`+`KeepAlive` true olduğu için bilgisayar yeniden başlayınca zaten
otomatik açılırlar — sadece ilk kurulumda/manuel durdurulduysa elle
bootstrap etmek gerekir.

## 7. Bilinen açık konular

- `bot_red_card` ve `bot_attack_volume`: baktıkları veri alanı canlı
  API'den hiç dolmadığı için pratikte hiç çalışmıyor.
- xG botları: veri sadece **%13** oranında geliyor, çoğu maçta
  `insufficient_data` ile çekiliyor.
- `v4_api_bot.py` içinde bir istatistik-anahtarı keşif mekanizması var;
  Railway loglarında `"YENI ISTATISTIK ANAHTARI"` satırları, API'nin
  aslında hangi alanları doldurduğunu gösterir — yukarıdaki botları
  düzeltmenin yolu oradan geçiyor.
- **Bloke takımlar (2026-08-30, şike riski, kullanıcı talebi):** Alianza FC,
  Atlético Balboa, Junior (Barranquilla), (Independiente) Santa Fe, Vasco da
  Gama, Cruzeiro — `match_filter.py` `BLOCKED_TEAM_SUBSTRINGS` /
  `BLOCKED_TEAM_EXACT`. `is_known_match()` bunları lig bloklarından hemen
  sonra, KNOWN_TEAMS/arşiv kademelerinden ÖNCE reddeder. Vasco + Cruzeiro
  KNOWN_TEAMS'ten de çıkarıldı.

- **K3 League (Güney Kore, izleniyor, 2026-08-29):** NPL/Capital Football
  gibi bilerek bloke EDİLMEDİ — hiçbir şike iddiası/kötü ölçüm yok, sadece
  yapısal olarak ayni risk kategorisinde (yarı-profesyonel, alt kademe).
  Kullanıcı kararı: kanıt gelmeden bloke etme, birkaç hafta gerçek
  performansını izle. İlk sinyal 2026-08-29'da (Dangjin Citizen-Chuncheon,
  o an PENDING) - örneklem büyüyünce `bot-sinyalleri`de "k3 league" filtrele,
  isabet oranı taban oranların (kural 3) belirgin altındaysa bloke listesine
  ekle (bkz. `match_filter.py` BLOCKED_LEAGUE_NAMES, NPL Victoria orneği).
