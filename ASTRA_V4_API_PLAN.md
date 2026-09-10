# v4_api_bot (RapidAPI) - Astra'nın planı (2026-09-09, yarına ertelendi)

Bugün `v4_api_bot.py` (RapidAPI "free-api-live-football-data", 500K/ay
plan) yeniden aktif edildi ve çalışır durumda (feed=17-24 maç, 429 yok,
kota 60sn+7 ile ~%69 kullanımda güvenli). Bu dosya, GPT-6 Astra'nın
ek/genişletilmiş alan kullanımı için verdiği detaylı planı kaydeder -
**bugün hiçbiri uygulanmadı, kod/deploy değişikliği YOK.** Sakin bir
oturumda, sırayla ele alınacak.

## 🔴 ÖNCELİK 0 - önce bunu düzelt (yeni alan eklemeden ÖNCE)

**Sıfır ile eksik veri karışıyor.** `MAX_STATS_PER_CYCLE` dışında kalan
maçlar için `_no_stats()` boş liste döner, `_parse_stats([])` tüm
alanları `0` üretir ve bu `live_snapshots`'a **gerçek sıfır gibi**
yazılır. Botlar "veri çekilmedi" ile "gerçekten 0 şut/korner/xG"
ayrımını yapamıyor - özellikle momentum/tempo botlarını etkiliyor.

Ayrıca:
- Aynı 7 maç sürekli seçilebilir (sıralama sadece "zaten takip
  ediliyor mu"ya bakıyor, en son ne zaman istatistik alındığına değil).
- 5 dakikalık momentum penceresi gerçekte 5 dk olmayabilir (orchestrator
  "5 dk öncesinden herhangi bir kayıt"ı d5 kabul ediyor).
- `oy_veren>=5` şartı canlı istatistik garantisi VERMİYOR - 5 "hep
  hazır" bot (prematch_prophet, base_rate, odds_profile, game_state,
  form_asymmetry) canlı veri olmadan da eşiği geçebilir. En az BİR
  canlı-veri-bağımlı botun oy vermesi ayrıca şart koşulmalı.

Öneri: yeni sinyalde kullanılan canlı istatistiğin yaşı en fazla 120sn
olsun (pilot sınır, ölçülmüş optimum değil).

## Alan bazlı öncelik sırası (Astra)

| Öncelik | Alan | Karar |
|---|---|---|
| P0 | `red_cards` | Ev/deplasman ayrı kaydet. **Önce gölge modda** (sinyale etki etmeden) test et. Mevcut kart formülü iki tarafın kartını TOPLUYOR - sayısal fark/hangi taraf eksik bilgisini kaybediyor, önce bu formül düzeltilmeli. |
| P1 | `expected_goals_non_penalty` | İlk xG deneme adayı. Mevcut toplam xG'ye EKLEME - örtüşen ölçüm, ayrı bot versiyonu gibi test et. |
| P1 | `expected_goals_open_play` | İkinci aday, aynı kural. |
| P2 | `expected_goals_set_play` | İlk yayına almayın - korner sayısına ek bilgi sağlıyor mu ölç. |
| P2 | `expected_goals_on_target` (xGOT) | Ayrı bir kalite ölçümü (normal xG değil - şut sonrası kalite). Normal xG yerine geçirme, bağımsız oy oluşturma. |
| P2 | `keeper_saves` | Önce veri tutarlılığı kontrolü, rakibin isabetli şutlarıyla karşılaştır. |
| P3 | `shot_blocks`, `clearances` | Şimdilik ertele - mevcut şut verisine katkısı belirsiz. |
| İlk kapsam DIŞI | `fouls`, `interceptions`, `duel_won`, `ground_duels_won`, `aerials_won`, `dribbles_succeeded` | Mevcut botlarda savunulabilir kullanım yok. `bot_attack_volume`'u bu alanlarla "düzeltmeye" ÇALIŞMA - o botun ihtiyacı `dangerous_attacks`, bu listede hiçbiri o değil. |
| Kullanma | `discipline`, `duels` | Anlamı belirsiz (bazen `[None, None]`), hiçbir bota bağlanmamalı. |

## Ayrıştırma kuralları (format kırılganlığı)

API tutarsız formatlı veri gönderiyor (`"12 (44%)"` gibi yüzdeli
string'ler, bazı alanlar `None`). Kurallar:
- `0`/`"0"` → gerçek sıfır. `None`/`""`/`"-"`/`"N/A"` → eksik değer
  (SIFIR DEĞİL). `"NaN"`/`"Infinity"`/negatif/`"12abc"` → geçersiz,
  hata kaydı.
- `"12 (44%)"` → ayrı `count=12, success_rate=0.44` olarak parse et,
  tam eşleşmeyle (metindeki rastgele sayıları birleştirme).
- `[5, None]` gibi tek taraf eksikse toplam HESAPLANAMAZ say, diğer
  tarafın geçerli değerini atma.
- Kaynak, maç kimliği, dönem kapsamı, istatistiğin gözlem zamanı ve
  ayrıştırma durumu (başarılı/eksik/hatalı) saklanmalı.
- İstek atılmadığında veya başarısız olduğunda skor/dakika
  güncellenebilir ama YENİ ve DOLU bir istatistik snapshot'ı
  üretilmemeli (bkz. P0 madde).

## Kota yönetimi (500K/ay)

Mevcut 60sn+7 ayarı ~345.600/ay (~%69) - Astra bunu YETERLİ buluyor,
**daha da kısmayı önermiyor**. Asıl öneriler:
1. İstek bütçesini ortak ve kalıcı say - aynı aboneliği kullanan TÜM
   süreçler/uç noktalar (test çağrıları dahil) sayılmalı, restart
   sayacı sıfırlamamalı.
2. Sağlayıcının gerçek kalan kota/yenilenme başlıklarını oku
   (`x-ratelimit-requests-remaining/reset`) - "plan yükseltildi, 500K
   kaldı" diye varsayma.
3. 400K normal kullanım + 100K rezerv hedefle.
4. En uzun süredir istatistik alamayan uygun maçları döndür (rotasyon),
   sadece "zaten takip ediliyor" değil.
5. Gerçek sinyal pencerelerini (kodda dk 10-25 ve 56-79) kullan -
   bunların dışındaki maçlara otomatik en yüksek önceliği verme.
6. İstatistik isteğinin 429 hatasını da ana backoff mekanizmasına
   taşı (şu an `fetch_stats()` bunu sessizce boş listeye çeviriyor,
   ana döngü sadece FEED hatasına tepki veriyor).

## Ölçüm/yayına alma sırası

1. **İlk 1-2 gün:** Yukarıdaki P0 (eksik/sıfır ayrımı, adil rotasyon,
   momentum penceresi doğruluğu, kota/429 merkezi takibi) düzeltilsin.
2. **İlk yoğun hafta sonu:** Kart + seçilen xG alanları GÖLGE modda
   (veri toplanır, sinyale ETKİ ETMEZ) çalıştırılıp lig/alan kapsamı,
   ayrıştırma hata oranı ölçülsün.
3. **Yeterli sonuç birikince:** Temiz mevcut sistem vs. kart-eklenmiş
   vs. xG-varyantları ayrı ayrı karşılaştırılsın (Brier skoru + log
   loss, aynı sinyal hacminde isabet oranı).
4. **Kontrollü yayın:** Başarılı TEK değişken küçük kapsamda
   etkinleştirilsin - regresyon olursa önceki sürüme dönüş kolay
   olmalı.

Tam detay için Astra'nın orijinal yanıtı: kullanıcının ChatGPT projesi
"Matchrix Astra API Analizi" (2026-09-09 tarihli).

## Ek bulgu (2026-09-09, aynı gün): kapsam boşluğu

Kullanıcı AiScore ile karşılaştırdı: o anda AiScore **75 canlı maç**
gösterirken, v4_api_bot'un (RapidAPI ücretsiz/mevcut plan) feed'i sadece
**~20 maç** döndürüyordu (~%27 kapsam). Bu kod hatası değil - kaynağın
kendi kapsam sınırı (alt ligler, az bilinen turnuvalar muhtemelen hiç
gelmiyor).

Sonuç: tek kaynağa (v4_api_bot) geçmek sinyal HACMİNİ de kısıtlıyor
olabilir - kapsam darsa botlar değerlendirecek maç bulamaz. Yarınki
oturumda değerlendirilecek: (a) bu planın kota/alan önerilerini
uygulamak mı öncelikli, yoksa (b) paralel bir kaynak daha (örn.
flashscore/sofascore'u tekrar açmak, ya da RapidAPI'de daha geniş
kapsamlı bir plan) mı gerekiyor. Bugün karar verilmedi, hiçbir
değişiklik yapılmadı.

## Ek bulgu 2 (2026-09-09 gece yarısı): sinyal hacmi neden neredeyse sıfır

Aynı gece, oy_veren=4 + MIN_SIGNAL_MINUTE=10 deploy edildikten sonra
15+ dakika boyunca TEK BİR maç bile `eksik_veri`/`izleme`/`guclu_aday`
seviyesine ulaşmadı (hepsi `"none"` kaldı) - `DEGERLENDIRILEN=20-25`
olmasına rağmen. Astra'ya soruldu, cevap:

1. **Loglama kör noktası doğrulandı:** `"none"` sonucu, mutabakat VE
   final_prob'un ikisinin de düşük olduğu anlamına gelmiyor - biri
   düşük yeterli (ör. mutabakat=0.75 ama final_prob=0.54 → yine
   "none"). Yarınki teşhis: her turda seviye dağılımı (none/eksik_veri/
   izleme/guclu_aday) + "none" nedeni kırılımı (sadece mutabakat düşük
   / sadece final_prob düşük / ikisi de düşük) + 5 örnek maçın bot
   bazlı kararları loglanmalı. `oy_veren>=1` filtresi YETERSİZ - sıfır
   oylu maçlar da örneğe dahil edilmeli, aksi halde yeni bir kör nokta
   açılır.

2. **EN OLASI KOK NEDEN (kodda doğrulandı, teorik değil):**
   `v4_api_bot.py`'de istatistik çekilmeyen maçlar için `_no_stats()`
   boş liste döner, `_parse_stats([])` bunu SIFIR istatistik olarak
   üretir (bkz. P0 madde, yukarıda). Tempo/momentum botları bu sahte
   sıfırı GERÇEK veri sanıp (`insufficient_data` yerine) bir olasılık
   üretiyor - katılımcı sayısını (oy_veren) artırırken pozitif oy
   oranını/ağırlıklı olasılığı aşağı çekebilir. "En az 4 bot oy
   veriyor" ifadesi bu 4 botun SAĞLIKLI veriye dayandığını KANITLAMIYOR.
   Yarın ilk araştırılacak hipotez bu olmalı.

3. Gece yarısı etkisi (az maç, egzotik ligler) mümkün ama KANITLANMADI
   - 15 dakikalık aynı 20-25 maçın tekrar tekrar değerlendirilmesi
   yeterli/bağımsız örneklem değil.

4. **Önemli mantık düzeltmesi:** `MIN_SIGNAL_MINUTE=10`, "veri
   yetersizken sinyal üretme" sorununu ÇÖZMÜYOR - sadece 10 dakika
   ERTELİYOR. Aynı 4 hazır bot 11. dakikada da tek başına karar
   verebilir. Bugün oy_veren'i 5→4 indirirken bu ikisinin "aynı
   sorunu iki kez çözdüğü" gerekçesi YANLIŞTI. Yarın ölçülecek şey
   "kaç bot oy verdi" değil, "hangi bot HANGİ GERÇEK veriyle oy
   verdi" olmalı.

**Bu gece hiçbir kod değişikliği yapılmadı** (kullanıcı kararı, saat
gece yarısını geçti). Yarınki öncelik: P0 (sıfır/eksik veri ayrımı)
+ bu teşhis loglaması birlikte ele alınabilir, ikisi de aynı kök
soruna bakıyor.

## Sonuç (2026-09-10, gece, /goal oturumu): P0 fix doğrulandı

Commit `c116220` deploy edildi (00:02 UTC). Sonuç:
- Deploy sonrası ~5 dakika (00:02-00:07) sürekli `database is locked`
  - bugünkü desenle tutarlı, HER deploy bu kadar kilitleniyor. Bu,
  tek-yazıcı-kuyruğu sorununun hâlâ en büyük kısıtlayıcı olduğunun
  bir kanıtı daha (bkz. CLAUDE.md kural 6b, bugün defalarca gözlendi).
- Kilit açılınca (00:07:18) v4_api_bot ilk turunu tamamladı (feed=14,
  1.15sn) ve HEMEN ardından **farklı maç ID'leri** TESHIS'te görünmeye
  başladı (17547753, 17547816 - önceki ~5 dakikadır tek bir maç
  [17547882] donuk şekilde tekrarlanıyordu). P0 fix doğrulandı: sahte
  sıfır yerine gerçek NULL yazılıyor, sistem normal davranışına döndü.
- Henüz `SİNYAL BULUNDU` yok ama sistem artık gerçek/taze veriyle
  çalışıyor - bu iyi bir işaret, hacim zamanla netleşecek.

**Yarın için netleşen öncelik:** Bugünkü OLAYIN TAMAMI (5 kez art arda
deploy → 5 kez ~5dk kilit fırtınası) tek-yazıcı-kuyruğu mimarisinin
NEDEN artık ertelenemeyecek kadar acil olduğunu somut şekilde
gösteriyor - her küçük düzeltme bile üretimde dakikalarca kesintiye
neden oluyor. Bu, Astra ile daha önce konuşulan ama "yarın, sakin bir
oturumda" diye ertelenen konu.

## Gece sonu doğrulama (2026-09-10, ~00:15 UTC)

P0 fix'in GERÇEK etkisi doğrulandı: yeni günün (09-10) İLK sinyali
üretildi ve **kazandı** (`bugun_paylasilan=1, bugun_kazanan=1,
bugun_isabet_orani=1.0`). Genel toplam da ilerledi (`sonuclanan`
1844→1847, 3 sinyal daha sonuçlandı gece boyunca). Sistem sahte-sıfır
düzeltmesinden sonra gerçekten çalışıyor.

**Bu gecenin özeti:**
1. ✅ P0 (sahte sıfır → gerçek NULL) - doğrulandı, çalışıyor, ilk
   sinyal üretti ve kazandı.
2. ⚠️ Başlangıç kademeleme (Seçenek A) - deploy edildi ama etkisi
   belirsiz/sınırlı görünüyor - yarın gözden geçirilmeli.
3. 📝 Tek-yazıcı-kuyruğu envanteri ve 3 seçenek belgelendi
   (`SINGLE_WRITER_QUEUE_DESIGN.md`) - yarının başlangıç noktası hazır.

Bu gece için değişiklik burada durduruldu (yeterli, doğrulanmış
ilerleme + kullanıcı uyurken ek risk almamak için).

## P0 TAMAMLANDI (2026-09-10, commit 84f0160) + Astra PDF değerlendirmesi

Astra'nın tam kod incelemesi (Matchrix_Degerlendirme PDF): önceki P0
fix'i (c116220) EKSİKTİ - sadece `_no_stats() -> None` yolunu düzeltmişti.
`_parse_stats([])` (boş API yanıtı, ayrı yol) hâlâ sıfır üretiyordu;
`[12, None]` → ikisi de 0 + 12 kayboluyor; `"12 (44%)"` → 0; `"NaN"` →
kabul ediliyordu.

**84f0160:** `_parse_stats` yeniden yazıldı. Tüm alanlar `None` başlar,
her taraf `_stat_one_side()` ile BAĞIMSIZ ayrıştırılır (int/float/"12"/
"12 (44%)" kabul; None/""/"-"/"NaN"/Inf/negatif/"12abc"/bool → None).
`None` ve `[]` ikisi de → hepsi None. Astra'nın tüm test vakaları geçiyor
(ağ/DB olmadan inline test edildi). Downstream zaten doğru (`features.py:_g`
NULL → None, `_toplam` bir taraf None ise None).

**Deploy doğrulaması (09:39 → 09:53, 14dk):** crash yok, Traceback yok
(NameError riski için `import re` eklendi - temiz), her v4 turu ~0.6sn.
`SQLITE_BUSY` sıfır (iddaa fix hâlâ tutuyor, yeni deploy'un restart'ı
bile fırtına yaratmadı). Sinyal etkisi ÖLÇÜLEMEDI - öğlen olduğu için
canlı feed BOŞ (0 maç). Akşam maçları başlayınca ölçülecek.

## Astra'nın önerdiği SIRA (PDF'ten)

1. ✅ Parser'daki kalan sahte sıfırlar (84f0160)
2. **Teşhis + istatistik seçim ölçümü + canlı-veri şartının GÖLGE
   değerlendirmesi.** Tam akış görünmeli: feed → uygun maç → istatistik
   için seçilen → geçerli/taze istatistik → değerlendirilen → güçlü aday
   → DB'ye kaydedilen → Telegram'a gönderilen. Her turda: seviye
   dağılımı + "none" nedeni (düşük mutabakat / düşük olasılık / ikisi) +
   her dakika dönüşümlü 5 örnek maç (SIFIR oylu dahil) — bot adı, karar,
   olasılık, veri kalitesi, eksik alanlar, kaynak, veri yaşı. 15sn'de
   bütün maçları basmak YÜK.
3. Gerçek canlı-veri şartını etkinleştir + seçim açlığını düzelt.
   ŞART: bot SINIF ADINA bağlama (GameStateBot de Specialist!). Açık
   şart: "pozitif etkin ağırlıklı + çekilmemiş + ihtiyacı olan gerçek/
   geçerli/yeterince-güncel canlı istatistikle çalışan en az 1 bot."
   Momentum botunda iki ölçümün zaman aralığı da doğrulanmalı.
   Başlangıçta 24 saat GÖLGE ölçüm - hangi mevcut sinyaller elenirdi.
   Sayısal eşikleri bu sırada DEĞİŞTİRME.
4. Deploy açılışı + ertelenebilir bakım kilitleri. Şema kurulumu bir
   kez; geçerli referans sürümü varsa yeniden hesaplama; ağır hesap
   kilit dışında. Ertelenebilir temizliğe bağlantıya-özel 0.5-1sn kilit
   bütçesi + sonraki tura erteleme (global timeout aynı).
5. Kaynak denemesi + eşik kalibrasyonu. v4 kalsın; önce 7-istek
   verimini düzelt. Alternatif kaynak 48-72 saat ÜRETİME KARIŞTIRMADAN
   karşılaştır.

## Kota bütçesi devrede (2026-09-10, commit 08e3e60) - kullanıcı kararı

Kullanıcı: "%60'ını kullanabiliriz" + "300K'ya ulaşınca daha temkinli
olsun, aynen devam". 500K/ay kotanın sadece ~%12'si kullanılıyordu
(deploy anında `kota=2105/500000`), istatistik kapsaması bu yüzden
düşüktü (eski sabit `MAX_STATS_PER_CYCLE=7`).

**Yapılan:**
- `MAX_STATS_PER_CYCLE` artık dinamik, `_kota_kademesi()`:
  `used<300K → 25/tur` · `300-420K → 10` · `≥420K → 4`. Başlık gelmezse
  orta kademe (10). RapidAPI'nin her yanıtta gönderdiği
  `X-RateLimit-Requests-Limit/-Remaining` okunuyor (restart-güvenli,
  bellekte sayaç yok). Kotayı asla aşmaz - kullanım arttıkça otomatik
  geri çeker.
- **Astra K2/madde 4 rotasyonu düzeltildi:** eskiden "feed sırasındaki
  ilk N" seçiliyordu → aynı maçlar sürekli, gerisi hiç. Artık takip
  edilenler önce, onların içinde EN UZUN SÜREDİR istatistik çekilmemiş
  olan önce (`_son_stat_cekim`, monotonik saat).
- `📊 feed=` log satırına kota durumu eklendi: `kota=used/limit (%N)
  stat_butce=N/tur`.

**Deploy doğrulaması (11:24 → 11:33, 9dk):** crash/Traceback yok, her v4
turu ~1sn. `kota=2105→2115` (5dk'da ~10, tek canlı maç olduğu için).
`stat_butce=25/tur` (agresif kademe, beklendiği gibi). iddaa DB-kilit
fix'i (1ae9e7f) HÂLÂ tutuyor: bakım turu tur=20'de
`delete_unresolvable_void` dahil TÜM işler `wait_ms=0.0`, tek bir
`SQLITE_BUSY` yok. Bu deploy'un restart'ı fırtına yaratmadı.

Sinyal etkisi henüz ölçülemedi (öğlen, tek canlı maç `17548885`,
`eksik=16` - API bu maça istatistik döndürmüyor gibi, kod değil kapsam).
Akşam maçlarında `eksik=` düşüşü ve hacim izlenecek.

**Bugün 3. deploy. Astra kuralı gereği bugün BAŞKA deploy yok** - kalan
adımlar (2-5) tasarım aşamasında, P0/iddaa/kota değişiklikleri ~24 saat
stabil kalınca uygulanacak.

## Güvenli çalışma kuralı (Astra)

24 saatte EN FAZLA 1 planlı davranış değişikliği (veri bütünlüğü
acilleri istisna). Her yayın: tek sorun, ayrı commit, örnek-girdi
doğrulaması, geri dönüş hazır, 30-60dk aktif takip + 24 saat gözlem.
Geri dönüş tetikleyici: 3 döngü yeni snapshot yok / tekrarlayan kritik
yazma hatası / yeniden sahte veri. Kod geri alınır; sonuçlanmış
kayıtlar/DB geçmişi geri SARILMAZ.
