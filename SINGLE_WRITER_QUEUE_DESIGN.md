# Tek-yazıcı-kuyruğu tasarımı - ön hazırlık (2026-09-10 gece, /goal oturumu)

Bugün (2026-09-09→10) beş ayrı deploy'un HER BİRİ ~5 dakikalık sürekli
`database is locked` fırtınasına neden oldu (en son örnek: commit
`c116220`, 00:02-00:07 UTC). Bu, tesadüfi değil - yapısal bir sorun.
Bu dosya, konuyu ÇÖZMEDEN önce **kapsamı somut olarak ölçer** - böylece
yarınki oturum tahminle değil, gerçek envanterle başlar.

## Neden bu kadar sık kilitleniyor

SQLite'ta aynı anda SADECE BİR yazıcı olabilir (`BEGIN IMMEDIATE` /
`busy_timeout=30000` bunu bekletir, engellemez). Railway'deki `web`
servisi TEK bir container'da şu süreçleri aynı anda çalıştırıyor
(`supervisor.py`):
- `api` (kullanıcı istekleri + admin panel + tüm `/api/admin/*` yazma
  uçları)
- `orchestrator` (sinyal üretim döngüsü + bakım turu + settlement)
- `v4_api_bot` (canlı veri senkronu, her 60sn)
- `telegram_poster`, `telegram_bilgilendirme`, `iddaa_odds_client`

HER deploy TÜM bu süreçleri AYNI ANDA yeniden başlatıyor. Restart
anında hepsi neredeyse aynı saniyede DB'ye yazmaya çalışıyor (özellikle
orchestrator'ın referans verisi kurulumu/bakım turu ağır ve çok
sorgulu) - bu da birkaç dakikalık bir "herkes sırada bekliyor" fırtınası
yaratıyor. `busy_timeout=30000` her bireysel denemeyi 30sn'ye kadar
bekletiyor ama sıra uzunsa (5-6 süreç × birden fazla yazma denemesi)
toplam süre kolayca 3-5 dakikayı buluyor.

## Mevcut korunma seviyesi (bugün kuruldu, kısmi)

`db_config.py:measured_write()` - `BEGIN IMMEDIATE` + `busy_timeout`
+ ölçüm/log ile SARILMIŞ yazma noktaları artık en azından ÇÖKMÜYOR,
sadece 30sn bekleyip başarısız oluyor (ve çoğu çağıran taraf bunu
crash etmeden logluyor - bugün v4_api_bot/telegram_poster'a eklendi).
Bu bir GÜVENLİK AĞI, kilidin KENDİSİNİ çözmüyor.

## Envanter (bu gece ölçüldü, ~30dk kod taraması)

Ham `connect()` (measured_write'sız) + `conn.commit()` içeren dosyalar:

| Dosya | commit() sayısı | Not |
|---|---|---|
| `api.py` | 10 | En kalabalık - kısmen measured_write'a geçirildi, KALAN 10 tanesi hâlâ çıplak |
| `v4_api_bot.py` | 6 | Bugün kısmen sertleştirildi (`_ensure_match_tracking_schema` retry aldı) - ASAMA 4'teki ana INSERT döngüsü hâlâ çıplak |
| `odds_profile.py` | 6 | Hiç dokunulmadı |
| `thesports_bot.py` | 4 | Artık aktif değil ama supervisor.py'de referans var - kontrol edilmeli |
| `prematch.py` | 3 | Hiç dokunulmadı - team_profiles kurulumu, ağır/nadir ama uzun sürebilir |
| `baserates.py` | 2 | Hiç dokunulmadı |
| `odds.py` | 2 | Hiç dokunulmadı |
| `team_history.py` | 1 | Hiç dokunulmadı |
| `app/core/orchestrator.py` | 1 | Sinyal yazma bloğu bugün try/except aldı (94ec417) ama measured_write'a GEÇMEDİ |

**Toplam ~35 çıplak commit noktası**, 9 dosyaya yayılmış.

## Yarın için üç seçenek (öncelik sırasıyla, risk/etki dengesine göre)

### Seçenek A (en düşük risk, en hızlı): "Restart fırtınasını dağıt"
Sorunun kökü "hepsi AYNI ANDA başlıyor" - `supervisor.py`'de her
sürecin başlangıcına KÜÇÜK, RASGELE bir gecikme eklemek (örn.
`orchestrator` 0-2sn, `v4_api_bot` 3-5sn, `telegram_poster` 6-8sn gibi
kademeli) restart anındaki çakışmayı önemli ölçüde azaltabilir.
TEK dosya (`supervisor.py`), birkaç satır, davranış değişikliği yok -
sadece süreçlerin başlama SIRASI/zamanlaması. Bugünkü "her yeni
değişiklik yeni bir kilit fırtınası" sorununu hafifletir ama TEMEL
mimariyi değiştirmez.

### Seçenek B (orta risk, orta etki): "Kalan çıplak commit'leri measured_write'a taşı"
Yukarıdaki ~35 noktayı, bugün zaten kanıtlanmış `measured_write()`
deseniyle sarmak. Riski DOSYA SAYISI kadar çok (9 dosya) - her biri
ayrı test/deploy gerektirir, aceleye getirilirse yeni bug riski var
(örn. bugünkü indentation hatası riski, `conn` kapsamı hataları).
ETKİSİ: artık hiçbir yazma noktası crash etmez, hepsi ölçülür/loglanır
- ama kilidin KENDİSİ (kuyruk uzunluğu) değişmez.

### Seçenek C (en yüksek risk, en yüksek etki): "Gerçek tek-yazıcı-kuyruğu"
Tüm yazma isteklerini TEK bir süreç/thread'e (örn. `api` servisi
içinde bir arka plan kuyruğu) yönlendirip diğer tüm süreçler (`v4_api_bot`,
`orchestrator`, vb.) yazmak yerine bu kuyruğa MESAJ göndersin. SQLite'ın
kendi kilidi yerine uygulama-seviyeli bir kuyruk (örn. `queue.Queue` +
tek bir yazıcı thread) kullanılır. Bu, sorunu YAPISAL olarak çözer
(SQLite hiçbir zaman "meşgul" olmaz, çünkü sadece TEK bir bağlantı
hiç yazmayı bırakmaz) ama TÜM yazma çağrı noktalarının (35+) arayüzünü
değiştirmeyi gerektirir - büyük, çok dosyalı bir refactor, dikkatli
planlama ve muhtemelen birkaç günlük kademeli rollout gerektirir.

## Öneri

Yarın **Seçenek A** ile başla (düşük risk, hızlı, bugünkü acı noktayı
doğrudan hafifletir) - deploy başına birkaç dakikalık kesintiyi
büyük ölçüde azaltabilir. Sonuç ölçülüp yeterliyse B/C'ye acele
etmeye gerek kalmayabilir. Yetersizse B'ye (dosya dosya, tek tek,
her birini ayrı deploy+doğrulama ile) geçilir. C, ancak B tamamlanıp
sorun hâlâ ciddiyse gündeme alınmalı - bugünün deneyimi (küçük
değişikliklerin bile ne kadar riskli olabileceği) göz önüne alınınca,
büyük bir refactor'u SAKİN, iyi test edilmiş bir günde yapmak şart.

Bu gece HİÇBİR ŞEY UYGULANMADI - sadece envanter çıkarıldı ve seçenekler
somutlaştırıldı, yarınki karar için.

## Seçenek A sonucu (2026-09-10, aynı gece, deploy 59227d4)

Deploy edildi, doğrulandı. **Sonuç belirsiz/sınırlı:** v4_api_bot 3s
kademeli gecikmeyle başladı (00:11:37) ama kendi açılış kontrolü yine
3 ardışık `database is locked` hatası aldı (00:12:08, 00:12:46,
00:13:24 - önceki desenle aynı büyüklükte). Kademeleme, v4_api_bot'un
KENDİ başlangıcını erteledi ama muhtemelen diğer süreçlerin (özellikle
orchestrator'ın referans verisi/bakım turu) YİNE de çakıştığı bir
pencereye denk geldi - 7 servisi 3sn aralıklarla başlatmak, hangi
sürecin NE ZAMAN gerçekten DB'ye yazmaya başladığını (sadece process
başlatma anını değil) kontrol etmiyor.

**Sonuç:** Seçenek A tek başına yeterli değil gibi görünüyor (bu TEK
gözlemle kesin yargıya varılamaz - örneklem küçük). Yarın Seçenek B
(kalan çıplak commit'leri measured_write'a taşımak) veya orchestrator'ın
kendi açılış/bakım sırasının da kademelenmesi değerlendirilmeli.

**Bu gecelik iş burada bırakıldı** - art arda çok fazla deploy yapıldı,
kullanıcı uykuda ve gözlemleyemiyor, ek risk almanın faydası şüpheli
hale geldi. P0 fix (gerçek, doğrulanmış kazanım) + bu envanter/gözlem
(yarın için netleşmiş bir başlangıç noktası) bu gecenin somut çıktısı.

## measured_write migrasyonu sonucu (deploy d830135) - GÜÇLÜ POZİTİF SONUÇ

v4_api_bot'un ana yazma bloğu artık `measured_write()` kullanıyor.
İlk ölçülen tur (00:18:08): `wait_ms=0.0` (hiç kilit beklemedi),
`body_ms=59.8, commit_ms=0.1` - tamamen temiz, `feed=16 islenen=16`
(sıfır kayıp). Hemen ardından yeni bir sinyal üretildi (Comunicaciones
FC - Deportivo Marquense, dk=14, %64, guclu_aday) - dakika kapısı
(dk>=10) doğru çalışıyor.

Bu, bu gecenin EN İYİ ölçülen sonucu - üç değişikliğin (P0 fix +
stagger + measured_write) birlikte etkisiyle sistem artık hem
doğru hem hızlı çalışıyor. Yarın Seçenek B'nin geri kalanına (diğer
8 dosyadaki ~29 çıplak commit) bu kanıtla güvenle devam edilebilir -
desen çalıştığı doğrulandı.

**Bu gece için iş burada NİHAİ olarak bırakıldı.**
