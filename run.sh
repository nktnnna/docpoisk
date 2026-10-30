#!/bin/bash
# Запуск DocSearch из терминала (после bash install.sh).
#   bash run.sh            — окно приложения
#   bash run.sh --browser  — открыть в браузере (если окно по какой-то причине не открывается)
cd "$(dirname "$0")/backend"
exec ../.venv/bin/python -m app.desktop "$@"
