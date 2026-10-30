"""Настройки системы. Всё задаётся через переменные окружения (файл .env)."""
import os
from pathlib import Path


def _load_env_files() -> None:
    """Подхватывает настройки из файлов KEY=VALUE. Уже заданные переменные окружения важнее."""
    candidates = [
        Path(os.path.expanduser(os.getenv("DATA_DIR", "~/DocPoisk"))) / "settings.env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_files()


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:3b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")

# Где хранить данные. В настольном приложении — папка в домашнем каталоге пользователя
# (называется DocPoisk по старому имени приложения — чтобы при обновлении данные остались на месте).
DATA_DIR = os.path.expanduser(os.getenv("DATA_DIR", "~/DocPoisk"))

# Векторная база Qdrant. Если QDRANT_URL пуст — встроенный режим (база лежит файлами в DATA_DIR,
# отдельный сервер не нужен). В Docker-варианте указывается адрес контейнера qdrant.
QDRANT_URL = os.getenv("QDRANT_URL", "")
QDRANT_PATH = os.getenv("QDRANT_PATH", os.path.join(DATA_DIR, "vectordb"))
COLLECTION = os.getenv("QDRANT_COLLECTION", "documents")

CHUNK_SIZE = _int("CHUNK_SIZE", 1000)          # символов в одном чанке
CHUNK_OVERLAP = _int("CHUNK_OVERLAP", 200)     # перекрытие соседних чанков
TOP_K = _int("TOP_K", 6)                       # сколько чанков отдавать LLM
MIN_SCORE = _float("MIN_SCORE", 0.35)          # порог косинусной близости
REL_SCORE_GAP = _float("REL_SCORE_GAP", 0.15)  # отсекать фрагменты хуже лучшего больше чем на столько
CANDIDATES = _int("CANDIDATES", 20)           # сколько кандидатов брать из каждого вида поиска до объединения
NEIGHBORS = _int("NEIGHBORS", 1)               # сколько соседних фрагментов до/после найденного добавлять
# Сколько символов документов максимум отдавать LLM. Вместе с историей диалога должно влезать
# в LLM_NUM_CTX токенов (≈3 символа на токен), иначе Ollama молча обрежет начало — и инструкции.
CONTEXT_CHARS = _int("CONTEXT_CHARS", 9000)
HISTORY_TURNS = _int("HISTORY_TURNS", 3)       # сколько прошлых пар «вопрос-ответ» учитывать в диалоге
CONDENSE_QUESTION = os.getenv("CONDENSE_QUESTION", "1") not in ("0", "false", "no")  # уточнять вопрос по диалогу
LLM_TEMPERATURE = _float("LLM_TEMPERATURE", 0.0)
LLM_NUM_CTX = _int("LLM_NUM_CTX", 8192)

STORAGE_DIR = os.getenv("STORAGE_DIR", os.path.join(DATA_DIR, "files"))  # копии загруженных файлов
INBOX_DIR = os.getenv("INBOX_DIR", os.path.join(DATA_DIR, "inbox"))       # папка для массовой загрузки
EMBED_BATCH = _int("EMBED_BATCH", 16)

# Вход по паролю. Пользователи, ключ подписи сессий и журнал входов лежат в AUTH_DIR
# (в Docker — общая папка для контейнеров ingest и chat). AUTH=off — только для разработки.
AUTH_DIR = os.path.expanduser(os.getenv("AUTH_DIR", os.path.join(DATA_DIR, "auth")))
AUTH_ENABLED = os.getenv("AUTH", "on").lower() not in ("off", "0", "false", "no")
SESSION_HOURS = _int("SESSION_HOURS", 12)
# Логи сервисов (по файлу на сервис, с ротацией) — их можно посмотреть и выгрузить в разделе «Админ»
LOG_DIR = os.path.expanduser(os.getenv("LOG_DIR", os.path.join(DATA_DIR, "logs")))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
