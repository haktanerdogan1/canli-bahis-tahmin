# İlk yarı 1-1: ikinci yarı bir gol daha deneyi

Entegrasyon: orkestratör her 30 saniyede `ShadowRunner` çalıştırır. Yeni API
çağrısı yoktur. Bot üretim konsensüsüne, bildirimlere ve sinyal tablolarına
bağlanmaz. `HT11_SHADOW_ENABLED=0` deneyi kapatır; varsayılan açıktır.
Yayın/deploy yapılmadan çalışan Railway sürümü değişmez.

## İlk veri kontrolü (11 Eylül 2026)

`data/iddaa_arsiv_YEDEK.csv` içinde tarih/maç/hedef sonucu tekilleştirilmiş
3.153 adet devresi 1-1 bitmiş maç bulundu. Kronolojik ilk 1.576 maçta
devreden SONRA herhangi bir gol oranı %77,09, sonraki 1.577 maçta %77,74.
Bunlar 60/65/70/75 sonrası başarı oranı DEĞİLDİR. Tarih-sınırı ayrımı olmayan
bu ön kontrol bir model doğrulaması da değildir. İki dönemde de en az beş
gözlemi olan takım sayısı sıfır; takım bazlı korelasyon ölçülemedi. Yerel canlı
DB'de snapshot sayısı sıfır. CLAUDE.md'deki r>=0,25 koşulu doğrulanmadı.
Bu nedenle entegre edilen sürüm yalnızca aday toplama/ölçüm botudur;
öğrenilmiş olasılık veya üretim oyu uydurmaz.

## Uygulanan kurallar

- Yalnız mevcut RapidAPI `v4_` maçları; gerçek `HT` durumunda görülen skor
  ayrı saklanır. 45. dakika veya sonradan görülen 1-1, devre skoru sayılmaz.
- Devreyi kaçıran restart/ilk açılış maçlarında aday üretmez. Gözlenen devre
  skoru tekrar başlatmada kalıcıdır. Devre arasındaki skor düzeltmesi işlenir.
- Yalnız devre 1-1, güncel taraf skorları en az 1 ve son feed gözlemi en
  fazla 120 saniye eski olan LIVE maçlar kabul edilir.
- 60,65,70,75 ayrı deney kollarıdır. Her biri için hedef dakika veya bir
  sonraki dakikada ilk gözlem kaydedilir. Kaçırılan dakika geri doldurulmaz.
- Her maç/her kol/sürüm için bir kayıt. Örneğin 5-1 -> MS 6,5 üst.
  Bu dört kayıt dört yayınlanmış bahis değildir; raporlar ayrı hesaplanır.
- Snapshot varsa ham ölçümler ve zaman damgaları araştırma için saklanır.
  Şut/xG'nin taze olduğu varsayılmaz ve ilk sürümde karar için kullanılmaz.
- Kaynak DB yalnız okunur. Deney `DATABASE_PATH` ile aynı klasördeki
  `ht11_shadow.sqlite3` dosyasına yazar; bu dosya da kalıcı volume/yedeğe dahildir.
- Kaynak FINISHED sonucu yalnız `observed_outcome` üretir. Kaynak maçları
  donmuş veri yüzünden kapatabildiği için bunlar doğrulanmış başarı sayılmaz.
  Bağımsız sonuç teyidiyle `outcome` bir kez yazılır; sonra değiştirilmez.
  Skor gerilemesi VOID sayılır. Terk edilen maçlar bekleyen/belirsiz kalır.
- Kilit/hata orkestratöre yayılmaz; 250ms DB kilit beklemesi sonrası ertelenir.
  Kaynak okuma sorguları ayrıca yaklaşık 500ms hesaplama bütçesiyle kesilir.

## Rapor ve sonuç doğrulama

Yönetici panelindeki **1–1 Devre Botu** sekmesi çalışma durumunu, izlenen
maçları, adayları ve her dakikanın ayrı başarısını gösterir. Açık sekme 30
saniyede yenilenir. Dakika/sonuç filtreleri vardır. Sonucu doğrula formunda
bağımsız kaynaktan maç sonu skoru ve kaynak açıklaması girilir; maçın bütün
bekleyen denemeleri sonuçlandırılır. Mevcut admin anahtarı her iki API ucunda
zorunludur. Doğrulanmış kayıtlar değiştirilmez.

Proje kökünden, canlı DB'nin yanındaki gerçek deney dosyasını belirtin:

```sh
python -m app.core.ht11_shadow --db /volume/ht11_shadow.sqlite3
python -m app.core.ht11_shadow --db /volume/ht11_shadow.sqlite3 --confirm-result v4_123 2 1 --evidence 'Bağımsız sonuç kaynağı, kontrol zamanı ve referansı'
python -m app.core.ht11_shadow --db /volume/ht11_shadow.sqlite3 --backtest
```

`/volume` örnektir; gerçek DATABASE_PATH klasörü kullanılır. Rapor `unverified`
ve doğrulanmış sonuçları ayırır. Her deney kolu ayrı raporlanır. Backtest tarih
bazında kronolojik ikiye böler, sınırı aşan maçları çıkarır. Eğitim hücresi
(kontrol dakikası, toplam gol, skor farkı) başına en az 60 örnek gerekir.
Test Brier skoru aynı test maçlarında yalnız dakika taban oranıyla karşılaştırılır.
Korelasyon hesaplanamıyorsa `null` kalır. Araç üretimi otomatik açmaz.

## Sonraki yayın kapısı

Önce yeterli doğrulanmış maç ve tarih kapsamı, sonra ayrı test döneminde
kalibrasyon/ek katkı incelemesi gerekir. En iyi dakikayı bu testte seçtikten
sonra aynı testi tekrar başarı kanıtı olarak kullanmayın; yeni dönem ayırın.
Canlı şut/xG katkısı ancak iki uçta gerçek zaman ve kapsam doğrulanınca ayrı
denenir. Üretim entegrasyonu bu kanıt olmadan etkinleştirilmez.
