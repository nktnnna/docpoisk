#!/bin/bash
# Версия для разработчика (Docker, всё по отдельным контейнерам).
#   bash dev.sh up             собрать и запустить (код перезагружается при изменении).
#                              Если на компьютере уже работает Ollama (её ставит install.sh), контейнеры
#                              используют её: быстрее (видеоядро Mac) и модели уже скачаны.
#                              Свою Ollama в контейнере принудительно: OLLAMA_IN_DOCKER=1 bash dev.sh up
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

if [[ "${1:-help}" != "help" ]]; then
  command -v docker >/dev/null || { echo "Docker не установлен. Поставьте Docker Desktop: https://www.docker.com/products/docker-desktop/ и откройте его."; exit 1; }
  docker info >/dev/null 2>&1 || { echo "Docker не запущен: откройте приложение Docker Desktop и подождите, пока кит в строке меню перестанет мигать."; exit 1; }
fi
need_running() {
  "${DC[@]}" ps --status running --services 2>/dev/null | grep -qx ingest ||
    { echo "Контейнеры не запущены. Сначала выполните: bash dev.sh up"; exit 1; }
}

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
check_ports() {
  # свои уже запущенные контейнеры держат эти порты — это не конфликт
  "${DC[@]}" ps --status running --services 2>/dev/null | grep -qx ingest && return 0
  local bad=0 spec name port
  for spec in "WEB_PORT:${WEB_PORT:-8080}" "DEV_INGEST_PORT:${DEV_INGEST_PORT:-8001}" "DEV_CHAT_PORT:${DEV_CHAT_PORT:-8002}" \
              "DEV_QDRANT_PORT:${DEV_QDRANT_PORT:-6333}" ${1:+"DEV_OLLAMA_PORT:${DEV_OLLAMA_PORT:-11435}"}; do
    name=${spec%%:*}; port=${spec##*:}
    if port_busy "$port"; then
      echo "Порт $port уже занят другой программой. Запустите с другим портом, например:  $name=$((port + 10)) bash dev.sh up"
      bad=1
    fi
  done
  return $bad
}

case "${1:-help}" in
  up)    mkdir -p data/{files,inbox,auth,logs,exports}
         BUILD=--build; [[ "${2:-}" == "--no-build" ]] && BUILD=--no-build
         if [[ -z "${OLLAMA_IN_DOCKER:-}" ]] && curl -s --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
           echo "==> На компьютере уже работает Ollama — контейнеры будут использовать её (модели уже скачаны, работает быстрее)."
           export OLLAMA_URL=http://host.docker.internal:11434
           if [[ -z "${LLM_MODEL:-}" && -f "$HOME/DocPoisk/settings.env" ]]; then
             LLM_MODEL=$(sed -n 's/^LLM_MODEL=//p' "$HOME/DocPoisk/settings.env" | tail -1); export LLM_MODEL
           fi
           check_ports || exit 1
           "${DC[@]}" rm -sf ollama models >/dev/null 2>&1 || true   # контейнер Ollama не нужен
           "${DC[@]}" up -d $BUILD --no-deps web ingest chat qdrant
         else
           check_ports with-ollama || exit 1
           "${DC[@]}" up -d $BUILD
           echo "Модели скачиваются в контейнер ollama (≈3 ГБ, один раз): bash dev.sh logs models"
         fi
         echo; echo "Интерфейс: http://localhost:${WEB_PORT:-8080}"
         echo "API: http://127.0.0.1:${DEV_INGEST_PORT:-8001}/docs, http://127.0.0.1:${DEV_CHAT_PORT:-8002}/docs · база: http://127.0.0.1:${DEV_QDRANT_PORT:-6333}/dashboard"
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
  shell) need_running; "${DC[@]}" exec "${2:-ingest}" sh ;;
  admin-console)
         need_running; "${DC[@]}" exec ingest python -m app.console --url http://127.0.0.1:8000 ;;
  *)     sed -n '2,11p' "$0" ;;
esac
