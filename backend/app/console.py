"""Консоль администратора в терминале — те же команды, что в разделе «Админ» → «Консоль».

Запуск на Mac:  bash run.sh --console        (или: cd backend && ../.venv/bin/python -m app.console)
В Docker:       bash dev.sh admin-console

Если приложение запущено, консоль подключается к нему и работает через него (нужен вход
администратора) — так база не конфликтует, а действия попадают в журнал под вашим именем.
Если приложение закрыто — работает с данными напрямую (автономный режим). В автономном режиме
вход не нужен: так можно восстановить забытый пароль командой passwd.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
from pathlib import Path

import httpx

from . import config

PORT_FILE = Path(config.DATA_DIR) / "app.port"   # его пишет запущенное настольное приложение


def _find_app(url: str | None) -> tuple[str, str] | None:
    """Адрес запущенного приложения и префикс API ("/api" — настольное, "" — внутри контейнера)."""
    candidates = [url] if url else []
    if not url and PORT_FILE.exists():
        candidates.append(f"http://127.0.0.1:{PORT_FILE.read_text().strip()}")
    for base in candidates:
        for prefix in ("/api", ""):
            try:
                r = httpx.get(f"{base.rstrip('/')}{prefix}/auth/status", timeout=3)
                if r.status_code == 200 and "setup_needed" in r.json():
                    return base.rstrip("/"), prefix
            except (httpx.HTTPError, ValueError):
                continue
    return None


def remote(base: str, prefix: str) -> None:
    client = httpx.Client(base_url=base, timeout=600)
    st = client.get(f"{prefix}/auth/status").json()
    print(f"Подключено к запущенному приложению: {base}")
    if st["setup_needed"]:
        print("Администратор ещё не создан — откройте приложение и создайте его на экране первого запуска.")
        return
    if st["enabled"]:
        for _ in range(3):
            user = input("Имя администратора: ").strip()
            password = getpass.getpass("Пароль: ")
            r = client.post(f"{prefix}/auth/login", json={"username": user, "password": password})
            if r.status_code == 200:
                break
            print(r.json().get("detail", f"Ошибка {r.status_code}"))
        else:
            print("Не удалось войти. Если забыли пароль: закройте приложение и запустите консоль снова — "
                  "она откроется в автономном режиме, где работает команда passwd.")
            return
    print("Консоль администратора DocSearch. help — список команд, exit — выход.")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if line in ("exit", "quit", "выход"):
            return
        if not line:
            continue
        r = client.post(f"{prefix}/admin/console", json={"command": line})
        if r.status_code in (401, 403):
            print(r.json().get("detail", "Нет доступа"))
            return
        data = r.json()
        print(data.get("output", data))
        if data.get("suggest") and input(f"Выполнить «{data['suggest']}»? [д/Н] ").strip().lower() in ("д", "y", "да", "yes"):
            print(client.post(f"{prefix}/admin/console", json={"command": data["suggest"]}).json().get("output"))


async def offline() -> None:
    from . import admin_service, auth

    auth.current_user.set({"username": "terminal", "role": "admin"})
    auth.current_ip.set("локально")
    print("Приложение не запущено — консоль работает с данными напрямую (автономный режим).")
    print("Консоль администратора DocSearch. help — список команд, exit — выход.")
    while True:
        try:
            line = (await asyncio.to_thread(input, "> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if line in ("exit", "quit", "выход"):
            return
        if not line:
            continue
        try:
            print(await admin_service.run_command(line))
            if line.split()[0].lower() in ("meta", "link", "unlink", "risk", "reindex", "delete", "cons", "passwd"):
                auth.audit("Консоль (терминал)", line.split()[0] + (" …" if len(line.split()) > 1 else ""))
        except admin_service.ConsoleError as e:
            msg = str(e)
            print("Неизвестная команда. Список: help" if msg.startswith("__unknown__") else f"Ошибка: {msg}")
        except Exception as e:
            if "already accessed by another instance" in str(e):
                print("База занята запущенным приложением DocSearch. Закройте его или запустите консоль, "
                      "когда приложение открыто, командой bash run.sh --console — она подключится к нему.")
                return
            print(f"Ошибка: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Консоль администратора DocSearch")
    parser.add_argument("--url", default=os.getenv("DOCSEARCH_URL"), help="адрес запущенного приложения")
    parser.add_argument("--offline", action="store_true", help="работать с данными напрямую (приложение должно быть закрыто)")
    args = parser.parse_args()
    found = None if args.offline else _find_app(args.url)
    if found:
        remote(*found)
    elif args.url and not args.offline:
        print(f"Приложение по адресу {args.url} не отвечает.")
    else:
        asyncio.run(offline())


if __name__ == "__main__":
    main()
