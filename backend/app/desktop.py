"""Настольное приложение ДокПоиск.

Поднимает локальный сервер на 127.0.0.1 (наружу не виден) и открывает
интерфейс в собственном окне приложения — браузер не нужен.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import uvicorn

from . import config


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ollama_alive() -> bool:
    try:
        urllib.request.urlopen(f"{config.OLLAMA_URL}/api/tags", timeout=2)
        return True
    except Exception:
        return False


def ensure_ollama() -> None:
    """Если Ollama установлена, но не запущена — запускаем её сами."""
    if _ollama_alive():
        return
    if sys.platform == "darwin" and os.path.isdir("/Applications/Ollama.app"):
        subprocess.Popen(["open", "-g", "-a", "Ollama"])
    elif shutil.which("ollama"):
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        print("Ollama не найдена — интерфейс подскажет, что сделать.")
        return
    for _ in range(30):
        if _ollama_alive():
            return
        time.sleep(1)


def main() -> None:
    ensure_ollama()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config("app.main:app", host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}/"

    if "--browser" in sys.argv:  # запасной режим: открыть в браузере
        import webbrowser

        webbrowser.open(url)
        print(f"ДокПоиск работает: {url}  (Ctrl+C — выход)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True  # кнопка «Скачать» в базе знаний
    webview.create_window("ДокПоиск", url, width=1280, height=820, min_size=(760, 520))
    webview.start()
    server.should_exit = True


if __name__ == "__main__":
    main()
