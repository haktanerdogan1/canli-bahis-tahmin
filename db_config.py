"""
Veritabani yolunu tek yerden yoneten yardimci modul.

NEDEN GEREKLI:
  Railway'in dosya sistemi GECICIDIR. Her deploy'da konteyner sifirdan kurulur ve
  git deposundaki dosyalar geri yazilir. Veritabani dosyasi (database/fh_goal_predictor.db)
  git'te takipli oldugu icin, HER DEPLOY'DA canli veritabani depodaki eski surumle
  DEGISTIRILIYORDU. Bu, uyelik sistemiyle birlikte ciddi bir soruna donusur:
  siteye kaydolan kullanicilarin hesaplari bir sonraki deploy'da SILINIR.

COZUM:
  DATABASE_PATH ortam degiskeni tanimliysa (Railway'de kalici bir Volume'a isaret eder)
  veritabani orada tutulur ve deploy'lardan etkilenmez. Volume ilk kez bostaysa,
  depodaki veritabani bir kereye mahsus "tohum" olarak oraya kopyalanir.
"""
import os
import shutil

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_DB = os.path.join(PROJECT_DIR, 'database', 'fh_goal_predictor.db')


def get_db_path() -> str:
    target = os.environ.get("DATABASE_PATH")

    if not target:
        # Yerel gelistirme: depodaki dosyayi kullan
        return SEED_DB

    os.makedirs(os.path.dirname(target), exist_ok=True)

    # Kalici disk bos ise depodaki veritabanini bir kereligine kopyala
    if not os.path.exists(target) and os.path.exists(SEED_DB):
        try:
            shutil.copy2(SEED_DB, target)
            print(f"[db_config] Kalici diske ilk kurulum: {SEED_DB} -> {target}", flush=True)
        except Exception as e:
            print(f"[db_config] Tohum kopyalama basarisiz: {e}", flush=True)

    return target


DB_PATH = get_db_path()
