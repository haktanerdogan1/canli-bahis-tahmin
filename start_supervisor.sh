#!/bin/bash
# Bu script'i cift tiklamadan da calistirabilirsin:
#   chmod +x start_supervisor.sh && ./start_supervisor.sh
# launchd (LaunchAgent) tarafindan da bu script cagriliyor.
set -e
cd "$(dirname "$0")"
exec python3 supervisor.py
