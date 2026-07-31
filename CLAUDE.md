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

## 4. `outcome` sütunu değiştirilemez

`consensus_predictions.outcome` bir kez yazıldıktan sonra **ASLA**
güncellenmez veya silinmez (bkz. `settlement.py` — `WHERE ... AND outcome
IS NULL` koruması bilerek var). Geçmiş sonuçlar bozulmamalı; sinyal
kalibrasyonu bu verinin bütünlüğüne dayanıyor.

## 5. Test sonrası temizlik

Test amacıyla `/tmp`'ye proje kopyası bırakılmaz. İş bitince kopya silinir.

## 6. Bilinen açık konular

- `bot_red_card` ve `bot_attack_volume`: baktıkları veri alanı canlı
  API'den hiç dolmadığı için pratikte hiç çalışmıyor.
- xG botları: veri sadece **%13** oranında geliyor, çoğu maçta
  `insufficient_data` ile çekiliyor.
- `v4_api_bot.py` içinde bir istatistik-anahtarı keşif mekanizması var;
  Railway loglarında `"YENI ISTATISTIK ANAHTARI"` satırları, API'nin
  aslında hangi alanları doldurduğunu gösterir — yukarıdaki botları
  düzeltmenin yolu oradan geçiyor.
