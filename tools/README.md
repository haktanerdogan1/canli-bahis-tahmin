# JCode Bahis Yardımcısı

Bu kullanıcı betiği JCode'un en yeni açık sinyalini bahis sayfasında arar,
maçı açar ve ilgili marketi vurgular. Önce ekrandaki canlı maç listesini,
gerekirse sitenin arama kutusunu kullanır. Seçime tıklamaz, kupon tutarı
girmez ve bahis göndermez.

## Kurulum

1. Railway servisinde `BET_ASSISTANT_TOKEN` adıyla uzun, rastgele bir ortam
   değişkeni oluşturun ve servisi yeniden dağıtın.
2. Mac App Store'dan ücretsiz ve açık kaynak `Userscripts` Safari
   eklentisini kurun: https://apps.apple.com/app/userscripts/id1463298887
3. Safari > Ayarlar > Eklentiler bölümünden `Userscripts` eklentisini açın
   ve `inagaming696.com` için izin verin.
4. Userscripts uygulamasında `New Javascript` seçin,
   `jcode_bet_assistant.user.js` içeriğini yapıştırıp kaydedin.
5. Userscripts araç çubuğu simgesinden `Enable Injection` seçeneğini açın.
6. Bahis sayfasını yenileyin. İlk açılışta betik Railway'deki
   anahtarı sorar.

Anahtarı kaynak koduna yazmayın veya başkasıyla paylaşmayın. Değiştirmek için
bahis sitesinin geliştirici konsolunda aşağıdaki komutu çalıştırın:

```js
localStorage.removeItem("jcode-bet-assistant-token")
```
