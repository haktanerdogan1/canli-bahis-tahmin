#!/bin/bash
# Tek seferlik kurulum: bagimliliklari kurar + launchd LaunchAgent kaydeder
# Kullanim:  cd "/Users/sebnem/Desktop/adsız klasör 2" && bash kurulum.sh
set -e
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
PLIST_NAME="com.haktan.canlibahis.supervisor.plist"

echo "==> 1/4 Python bagimliliklari kuruluyor..."
pip3 install -r requirements.txt || pip3 install -r requirements.txt --break-system-packages

echo "==> 2/4 LaunchAgent klasoru hazirlaniyor..."
mkdir -p ~/Library/LaunchAgents

echo "==> 3/4 Eski servis varsa durduruluyor..."
launchctl bootout "gui/$(id -u)/com.haktan.canlibahis.supervisor" 2>/dev/null || true

echo "==> 4/4 plist kopyalanip yukleniyor..."
cp "deploy/$PLIST_NAME" ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/"$PLIST_NAME"

sleep 2
echo
echo "==> Durum:"
launchctl list | grep canlibahis || echo "UYARI: servis listede gorunmuyor, logs/launchd_supervisor.err.log dosyasina bak"
echo
echo "Kurulum tamamlandi. Loglari izlemek icin:"
echo "  tail -f logs/*.log"
