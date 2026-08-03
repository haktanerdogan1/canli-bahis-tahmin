# Matchrix — Mobil Uygulama (iOS + Android)

Bu klasör, mevcut web sistemine (backend, API, `index.html`) **hiç dokunmadan**
onu native bir mağaza uygulamasına saran ayrı bir [Capacitor](https://capacitorjs.com)
projesidir. Uygulama açıldığında canlı siteyi
(`https://web-production-f1dba.up.railway.app`) yükler — yani backend'de veya
sitede yapılan her güncelleme, mağazaya yeni sürüm göndermeden otomatik olarak
uygulamaya da yansır.

## Neler hazır

- `capacitor.config.json` — uygulama canlı siteyi yükleyecek şekilde
  yapılandırıldı (`server.url`).
- `ios/` ve `android/` — her iki platformun native proje iskeleti oluşturuldu.
- Uygulama ikonu ve açılış ekranı (`resources/icon.png`, `resources/splash.png`)
  — koyu lacivert + camgöbeği renkleriyle, basit bir sinyal-dalgası motifiyle,
  **yer tutucu** olarak üretildi. Gerçek logon hazır olduğunda:
  1. `resources/icon.png` (1024×1024) ve `resources/splash.png` (2732×2732)
     dosyalarını değiştir.
  2. `npx capacitor-assets generate` çalıştır (tüm boyutları otomatik yeniden
     üretir).
  3. `npx cap sync` çalıştır.

## Senin yapman gereken adımlar (hesap/imzalama gerektirdiği için)

Bunları ben yapamam — kimlik doğrulama, ödeme ve mağaza hesabı gerektiriyor.

### iOS / App Store
1. Mac'ine **tam Xcode**'u App Store'dan kur (şu an sadece Command Line Tools
   var, tam Xcode gerekiyor).
2. [Apple Developer Program](https://developer.apple.com/programs/)'a üye ol
   (yıllık $99).
3. `mobile/ios/App/App.xcworkspace` dosyasını Xcode ile aç.
4. Xcode'da "Signing & Capabilities" sekmesinden kendi takımını (Apple
   Developer hesabını) seç, Bundle Identifier'ı (`com.jcodeanalytics.app`,
   istersen değiştir) onayla.
5. App Store Connect'te yeni bir uygulama kaydı oluştur, aynı Bundle ID'yi
   kullan.
6. Xcode > Product > Archive, ardından App Store Connect'e yükle.
7. TestFlight ile kendi cihazında test et, sonra inceleme için gönder.

### Android / Play Store
1. [Android Studio](https://developer.android.com/studio)'yu kur (JDK ve
   Android SDK'yı da beraberinde kurar).
2. [Google Play Console](https://play.google.com/console)'da geliştirici
   hesabı aç (tek seferlik $25).
3. `mobile/android` klasörünü Android Studio ile aç.
4. Play Console'da yeni bir uygulama kaydı oluştur.
5. Android Studio > Build > Generate Signed Bundle/APK ile imzalı bir
   `.aab` üret (imzalama anahtarını GÜVENLİ bir yerde sakla — kaybedersen
   uygulamayı bir daha güncelleyemezsin).
6. Play Console'da yüklenen `.aab`'yi bir sürüme bağla, incelemeye gönder.

## Yerel geliştirme / test

```bash
cd mobile
npm install
npx cap sync
npx cap open ios      # Xcode'da acar (tam Xcode gerekir)
npx cap open android  # Android Studio'da acar
```

`capacitor.config.json`'daki `server.url` canlı siteyi gösterdiği için,
simülatörde/telefonda açtığında doğrudan güncel siteyi göreceksin - ayrıca
bir "dev server" çalıştırmana gerek yok.

## Güvenlik notu

WebView varsayılan olarak yalnızca `server.url`'in alan adına gidebilir; site
içindeki dış bağlantılar (ör. ödeme sağlayıcı, sosyal medya) uygulama içinde
DEĞİL, cihazın kendi tarayıcısında açılır. Farklı bir alan adına izin vermek
gerekirse `capacitor.config.json`'a `server.allowNavigation` eklenmeli.
