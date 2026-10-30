#!/bin/bash
# Запуск DocSearch из терминала (после bash install.sh).
#   bash run.sh            — окно приложения
#   bash run.sh --browser  — открыть в браузере (если окно по какой-то причине не открывается)
#   bash run.sh --console  — консоль администратора в терминале (подключается к открытому приложению)
cd "$(dirname "$0")/backend"
if [[ "${1:-}" == "--console" ]]; then shift; exec ../.venv/bin/python -m app.console "$@"; fi
exec ../.venv/bin/python -m app.desktop "$@"
