"""Консоль администратора в терминале — те же команды, что в разделе «Админ» → «Консоль».

Запуск на Mac:  cd ~/docpoisk/backend && ../.venv/bin/python -m app.console
В Docker:       bash dev.sh admin-console
Работает напрямую с данными на этом компьютере, поэтому вход по паролю не нужен;
все изменения записываются в журнал от имени «terminal».
"""
from __future__ import annotations

import asyncio

from . import admin_service, auth


async def main() -> None:
    auth.current_user.set({"username": "terminal", "role": "admin"})
    auth.current_ip.set("локально")
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
            if line.split()[0].lower() in ("meta", "link", "unlink", "risk", "reindex", "delete", "cons"):
                auth.audit("Консоль (терминал)", line[:300])
        except admin_service.ConsoleError as e:
            msg = str(e)
            print("Неизвестная команда. Список: help" if msg.startswith("__unknown__") else f"Ошибка: {msg}")
        except Exception as e:
            print(f"Ошибка: {e}")


if __name__ == "__main__":
    asyncio.run(main())
