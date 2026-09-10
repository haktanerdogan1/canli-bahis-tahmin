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
