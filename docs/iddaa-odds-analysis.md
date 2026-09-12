# İddaa Oran Analizi

Admin paneli → **İddaa Oran Analizi**. Mevcut İddaa istemcisi her 5 dakikada
başlamamış maçların son görülen oranlarını gönderir. Panel açıkken dakikada
bir yenilenir; 15 dakikadan eski akış veya başlamış maç gösterilmez.

Kaynak: `data/iddaa_arsiv_YEDEK.csv`, 33.525 benzersiz sonuçlanmış maç,
2022-07-27–2026-07-28. KG sonucu FTHG>0 ve FTAG>0, 2,5 Üst sonucu toplam>2
olarak hesaplanır. CSV içindeki pKG/pOver tahminleri etiket olarak kullanılmaz.
Geçersiz oran/skor, yinelenen tarih+maç ve bugünden sonraki kayıtlar dışlanır.

1/X/2 ters oranları kendi toplamına, Üst/Alt ters oranları kendi toplamına
bölünerek marj kaldırılır. Ev/deplasman yönü korunur. Dört olasılık alanının
her birinde fark en fazla 0,05 olmalıdır. En yakın en fazla 300 komşu seçilir;
100 komşu yoksa yüzde üretilmez. %95 Wilson aralığı geçmiş örneklemdeki oranı
tanımlar; maç olasılığı kalibrasyonu veya bahis kazancı kanıtı değildir.
Arşiv açılış oranları ile son görülen güncel maç öncesi oranlar farklı
zamanlardaki fiyatlardır. Lig bilgisi CSV'de bulunmadığından lig filtresi yoktur.

## Kronolojik kontrol (2026-09-12)

Yeniden çalıştırma: `.venv/bin/python iddaa_analysis.py`.
Kesim 2026-02-07: 16.535 eğitim, 16.990 test, 16.328 yeterli komşulu test maçı.
Aynı gün iki tarafa bölünmez. Parametreler test sonuçlarına göre ayarlanmadı.

| Ölçüm | KG Var | 2,5 Üst |
|---|---:|---:|
| Brier (düşük daha iyi) | 0,24724 | 0,24460 |
| Eğitim genel sıklığı Brier | 0,24929 | 0,25009 |
| Tahmin/sonuç korelasyonu | 0,0913 | 0,1483 |
| Marjsız açılış piyasası Brier | — | 0,24368 |

2,5 Üst'te piyasa tabanı bu yöntemden daha iyi. Bu ölçüm takım bazlı
yarım-arşiv bot kabul testi değildir; yeni bot kabulü anlamına gelmez.
Sistem yalnızca tarihsel karşılaştırma aracı olarak entegredir, konsensusa
oy veya Telegram sinyali göndermez. Canlı performans iddiası yoktur.

## Saklama ve hata davranışı

`iddaa_current_snapshot.json` canlı DB ile aynı dizinde, atomik dosya değişimiyle
yazılır; ana SQLite yazma kilidi kullanılmaz. Oran arşivleme işlemleri bittikten
sonra kaydedilir. Yeni istemci tam 2,5 oranlarını ayrıca taşır; mevcut 1,5
tercihini değiştirmez. Başarılı boş akış listeyi temizler. Hatalı ağ yanıtı
yenilenmiş veri sayılmaz. Eski istemci timestamp göndermiyorsa yeni panel
bekleme durumunda kalır. API admin kimlik doğrulamasını kullanır.

Yerel doğrulama: `python -m unittest discover -s tests -v`.
