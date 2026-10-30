#!/bin/bash
# Установка DocSearch на macOS (Apple Silicon или Intel).
# Запуск:  bash install.sh   (повторный запуск = переустановка с сохранением данных)
# Нужен интернет только на время установки. Дальше всё работает офлайн.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
DATA_DIR="$HOME/DocPoisk"
APP_DIR="$HOME/Applications/DocSearch.app"
OLD_APP_DIR="$HOME/Applications/ДокПоиск.app"   # так приложение называлось раньше

say() { printf "\n\033[1;32m==>\033[0m %s\n" "$*"; }
die() { printf "\n\033[1;31mОшибка:\033[0m %s\n" "$*" >&2; exit 1; }

[[ "$(uname)" == "Darwin" ]] || die "Этот установщик — для macOS. На Linux/Windows см. README (раздел Docker)."

# ---------- 1. Python (через uv — ставит нужную версию Python сам, без Homebrew) ----------
if ! command -v uv >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/uv" ]]; then
  say "Устанавливаю uv (менеджер Python)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
UV="$(command -v uv)"

say "Создаю окружение Python 3.12 и ставлю библиотеки…"
"$UV" venv --python 3.12 --allow-existing "$ROOT/.venv"
"$UV" pip install --python "$ROOT/.venv/bin/python" -r "$ROOT/backend/requirements-desktop.txt"

# ---------- 2. Ollama (запускает нейросети локально, использует GPU Apple Silicon) ----------
if [[ ! -d /Applications/Ollama.app ]] && ! command -v ollama >/dev/null 2>&1; then
  say "Устанавливаю Ollama…"
  TMP="$(mktemp -d)"
  curl -L --fail -o "$TMP/Ollama.zip" https://ollama.com/download/Ollama-darwin.zip
  unzip -q "$TMP/Ollama.zip" -d "$TMP"
  mv "$TMP/Ollama.app" /Applications/ || die "Не удалось скопировать Ollama.app в /Applications"
  rm -rf "$TMP"
fi

OLLAMA_BIN="$(command -v ollama || echo /Applications/Ollama.app/Contents/Resources/ollama)"
if ! curl -s http://127.0.0.1:11434/api/tags >/dev/null; then
  say "Запускаю Ollama…"
  open -g -a Ollama 2>/dev/null || ("$OLLAMA_BIN" serve >/dev/null 2>&1 &)
  for _ in $(seq 1 60); do curl -s http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
  curl -s http://127.0.0.1:11434/api/tags >/dev/null || die "Ollama не запустилась. Откройте Ollama из Программ и повторите."
fi

# ---------- 3. Настройки и модели ----------
mkdir -p "$DATA_DIR/inbox"
if [[ ! -f "$DATA_DIR/settings.env" ]]; then
  RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
  if   (( RAM_GB >= 24 )); then LLM=qwen2.5:14b
  elif (( RAM_GB >= 16 )); then LLM=qwen2.5:7b
  else                          LLM=qwen2.5:3b
  fi
  say "Оперативной памяти: ${RAM_GB} ГБ → выбираю модель $LLM"
  cat > "$DATA_DIR/settings.env" <<EOF
# Настройки DocSearch. После изменения перезапустите приложение.
# LLM, которая формулирует ответы:  qwen2.5:3b (8 ГБ RAM) · qwen2.5:7b (16 ГБ) · qwen2.5:14b (24+ ГБ)
LLM_MODEL=$LLM
# Модель эмбеддингов. Если поменять — удалите папку vectordb и загрузите документы заново.
EMBED_MODEL=bge-m3
# Размер фрагмента (символов), перекрытие и сколько фрагментов давать модели
CHUNK_SIZE=1000
CHUNK_OVERLAP=200
TOP_K=6
EOF
fi
# shellcheck disable=SC1091
set -a; source "$DATA_DIR/settings.env"; set +a

for M in "$EMBED_MODEL" "$LLM_MODEL"; do
  say "Скачиваю модель $M (один раз)…"
  "$OLLAMA_BIN" pull "$M"
done

# ---------- 4. Приложение в ~/Applications (видно в Launchpad и Spotlight) ----------
say "Создаю приложение DocSearch…"
# Если открыта старая версия — закрываем, иначе она держит базу и новая не запустится
pkill -f "app.desktop" 2>/dev/null && sleep 1 || true
rm -rf "$APP_DIR" "$OLD_APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cat > "$APP_DIR/Contents/MacOS/DocPoisk" <<EOF
#!/bin/bash
cd "$ROOT/backend"
exec "$ROOT/.venv/bin/python" -m app.desktop >> "$DATA_DIR/app.log" 2>&1
EOF
chmod +x "$APP_DIR/Contents/MacOS/DocPoisk"
cat > "$APP_DIR/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>DocSearch</string>
  <key>CFBundleDisplayName</key><string>DocSearch</string>
  <key>CFBundleIconFile</key><string>DocSearch</string>
  <key>CFBundleIdentifier</key><string>local.docsearch</string>
  <key>CFBundleExecutable</key><string>DocPoisk</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

# Иконка приложения: из assets/icon.png собираем .icns встроенными средствами macOS
make_icon() {
  local set="$1/DocSearch.iconset"
  mkdir -p "$set"
  for S in 16 32 128 256 512; do
    sips -z $S $S "$ROOT/assets/icon.png" --out "$set/icon_${S}x${S}.png" >/dev/null || return 1
    sips -z $((S * 2)) $((S * 2)) "$ROOT/assets/icon.png" --out "$set/icon_${S}x${S}@2x.png" >/dev/null || return 1
  done
  iconutil -c icns "$set" -o "$APP_DIR/Contents/Resources/DocSearch.icns"
}
ICON_TMP="$(mktemp -d)"
if make_icon "$ICON_TMP"; then
  touch "$APP_DIR"   # чтобы Finder и Dock подхватили новую иконку
else
  echo "Не удалось собрать иконку — приложение будет со стандартной."
fi
rm -rf "$ICON_TMP"

say "Готово!"
cat <<EOF

  Приложение:   ~/Applications/DocSearch.app  (ищите «DocSearch» в Spotlight / Launchpad)
  Данные:       $DATA_DIR  (база, копии документов, настройки, лог)
  Массовая загрузка: положите файлы в $DATA_DIR/inbox и нажмите «из папки inbox»

Запускаю…
EOF
open "$APP_DIR"
