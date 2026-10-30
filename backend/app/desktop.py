"""Настольное приложение DocSearch.

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


ICON = os.path.join(os.path.dirname(__file__), "..", "..", "assets", "icon.png")


def _mac_branding() -> None:
    """Имя и иконка в Dock и меню: иначе macOS показывает «Python» с его значком."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle

        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = "DocSearch"
        NSApplication.sharedApplication().setApplicationIconImage_(
            NSImage.alloc().initWithContentsOfFile_(os.path.abspath(ICON)))
    except Exception:  # оформление необязательно — без него приложение работает так же
        pass


class SaveApi:
    """Сохранение файлов из окна приложения через обычный диалог «Сохранить» (вызывается из web/app.js).

    Встроенное окно само не умеет скачивать: без этого кнопка «Скачать» открывала бы файл
    (или ответ с ошибкой) вместо страницы приложения.
    """

    def save_file(self, filename: str, data_b64: str) -> str:
        import base64
        from pathlib import Path

        import webview

        target = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG, directory=str(Path.home() / "Downloads"), save_filename=filename)
        if not target:
            return ""
        path = target if isinstance(target, str) else target[0]
        Path(path).write_bytes(base64.b64decode(data_b64))
        return path


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
        print(f"DocSearch работает: {url}  (Ctrl+C — выход)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    import webview

    _mac_branding()
    webview.create_window("DocSearch", url, js_api=SaveApi(), width=1280, height=820, min_size=(760, 520))
    webview.start()
    server.should_exit = True


if __name__ == "__main__":
    main()
