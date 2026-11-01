#!/bin/bash
# Обновление DocSearch до последней версии. Документы, база и настройки сохраняются.
# Запуск:  bash update.sh
set -euo pipefail
cd "$(dirname "$0")"

if [[ -d .git ]]; then
  echo "==> Скачиваю обновления…"
  # fetch + reset вместо pull: срабатывает, даже если файлы программы случайно правили.
  # Затрагивает только папку программы; документы и база лежат в ~/DocPoisk.
  git remote set-url origin https://github.com/nktnnna/aiann.git   # программа переехала в этот репозиторий
  git fetch origin
  git reset --hard origin/main
else
  echo "Эта папка скачана ZIP-архивом, обновить её автоматически нельзя."
  echo "Скачайте новый ZIP с https://github.com/nktnnna/aiann и запустите в новой папке: bash install.sh"
  exit 1
fi

exec bash install.sh
