"""Логи в файлы: по файлу на сервис (desktop, ingest, chat), с ротацией, в config.LOG_DIR."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from . import config

FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
_done: set[str] = set()


def setup(service: str) -> None:
    if service in _done:
        return
    _done.add(service)
    os.makedirs(config.LOG_DIR, exist_ok=True)
    handler = RotatingFileHandler(os.path.join(config.LOG_DIR, f"{service}.log"), maxBytes=5_000_000, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter(FORMAT))
    root = logging.getLogger()
    root.setLevel(config.LOG_LEVEL)
    root.addHandler(handler)
    # у uvicorn свои логгеры без передачи в корневой — подключаем файл и к ним
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addHandler(handler)
