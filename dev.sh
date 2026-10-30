#!/bin/bash
# Версия для разработчика (Docker, всё по отдельным контейнерам).
#   bash dev.sh up             собрать и запустить (код перезагружается при изменении)
#   bash dev.sh down           остановить
#   bash dev.sh ps             состояние контейнеров
#   bash dev.sh logs [сервис]  логи в реальном времени (ingest, chat, web, qdrant, ollama)
#   bash dev.sh export-logs    все логи контейнеров + журнал входов и действий → архив в ./data/exports
#   bash dev.sh test           тесты внутри контейнера
#   bash dev.sh shell [сервис] командная строка внутри контейнера (по умолчанию ingest)
#   bash dev.sh admin-console  консоль администратора в терминале (те же команды, что в разделе «Админ»)
set -euo pipefail
cd "$(dirname "$0")"
DC=(docker compose -f docker-compose.yml -f docker-compose.dev.yml)

case "${1:-help}" in
  up)    mkdir -p data/{files,inbox,auth,logs,exports}; "${DC[@]}" up -d --build
         echo; echo "Интерфейс: http://localhost:${WEB_PORT:-8080}   API: http://127.0.0.1:8001/docs, http://127.0.0.1:8002/docs"
         echo "Первый вход: в интерфейсе создайте администратора." ;;
  down)  "${DC[@]}" down ;;
  ps)    "${DC[@]}" ps ;;
  logs)  "${DC[@]}" logs -f --tail=200 ${2:-} ;;
  export-logs)
         STAMP=$(date +%Y-%m-%d_%H-%M); OUT="data/exports/logs_$STAMP"; mkdir -p "$OUT"
         for s in $("${DC[@]}" config --services); do "${DC[@]}" logs --no-color --timestamps "$s" > "$OUT/container_$s.log" 2>&1 || true; done
         cp -r data/logs "$OUT/app_logs" 2>/dev/null || true
         cp data/auth/audit.jsonl "$OUT/audit.jsonl" 2>/dev/null || true
         tar -czf "$OUT.tar.gz" -C data/exports "logs_$STAMP" && rm -rf "$OUT"
         echo "Готово: $OUT.tar.gz" ;;
  test)  "${DC[@]}" run --rm --no-deps -e AUTH=on ingest sh -c "pip install -q pytest && python -m pytest -q tests" ;;
  shell) "${DC[@]}" exec "${2:-ingest}" sh ;;
  admin-console)
         "${DC[@]}" exec ingest python -m app.console ;;
  *)     sed -n '2,11p' "$0" ;;
esac
