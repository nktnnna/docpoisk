"""Вход, роли, журнал и консоль администратора — через настоящее приложение (без нейросетей)."""
import pytest
from fastapi.testclient import TestClient

from app import auth, config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    auth._fails.clear()
    from app.main import app
    return TestClient(app)


def login(c, name, password):
    return c.post("/api/auth/login", json={"username": name, "password": password})


def test_setup_login_and_protection(client):
    assert client.get("/api/auth/status").json()["setup_needed"] is True
    assert client.get("/api/ingest/documents").status_code == 401        # без входа — ничего не отдаём
    assert client.get("/api/chat/health").status_code == 401
    assert client.post("/api/auth/setup", json={"username": "anna", "password": "short"}).status_code == 400

    r = client.post("/api/auth/setup", json={"username": "anna", "password": "Secret-2024", "full_name": "Анна"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    st = client.get("/api/auth/status").json()
    assert st["user"]["username"] == "anna" and st["user"]["role"] == "admin"
    assert client.post("/api/auth/setup", json={"username": "x2", "password": "Secret-2024"}).status_code == 409

    stored = auth.load_users()["anna"]["password"]
    assert stored.startswith("scrypt$") and "Secret-2024" not in stored   # пароль не хранится в открытом виде

    client.post("/api/auth/logout")
    assert client.get("/api/auth/status").json()["user"] is None
    assert login(client, "anna", "wrong-pass").status_code == 401
    assert login(client, "anna", "Secret-2024").status_code == 200


def test_bruteforce_lock_and_audit(client):
    client.post("/api/auth/setup", json={"username": "anna", "password": "Secret-2024"})
    client.post("/api/auth/logout")
    for _ in range(auth.MAX_FAILS):
        assert login(client, "anna", "nope-nope").status_code == 401
    assert login(client, "anna", "Secret-2024").status_code == 429      # даже верный пароль — после блокировки
    actions = [e["action"] for e in auth.read_audit(50)]
    assert "Неудачный вход" in actions and "Вход заблокирован" in actions and "Создан первый администратор" in actions


def test_roles_and_password_change(client):
    client.post("/api/auth/setup", json={"username": "anna", "password": "Secret-2024"})
    assert client.post("/api/admin/users", json={"username": "ivan", "password": "Ivan-pass-1", "role": "user"}).status_code == 200
    assert client.patch("/api/admin/users/anna", json={"role": "user"}).status_code == 400   # последний админ

    user = TestClient(client.app)
    assert login(user, "ivan", "Ivan-pass-1").status_code == 200
    assert user.get("/api/admin/users").status_code == 403                # обычный пользователь — не админ
    assert user.post("/api/admin/console", json={"command": "users"}).status_code == 403

    # смена пароля завершает другие сессии
    other = TestClient(client.app)
    login(other, "ivan", "Ivan-pass-1")
    assert user.post("/api/auth/password", json={"old_password": "Ivan-pass-1", "new_password": "Ivan-pass-2"}).status_code == 200
    assert user.get("/api/auth/status").json()["user"]["username"] == "ivan"
    assert other.get("/api/auth/status").json()["user"] is None

    # блокировка пользователя администратором
    client.patch("/api/admin/users/ivan", json={"disabled": True})
    assert user.get("/api/auth/status").json()["user"] is None
    assert login(user, "ivan", "Ivan-pass-2").status_code == 401


def test_console_and_logs(client):
    client.post("/api/auth/setup", json={"username": "anna", "password": "Secret-2024"})
    out = client.post("/api/admin/console", json={"command": "help"}).json()
    assert out["ok"] and "meta <id> set" in out["output"]
    assert "anna" in client.post("/api/admin/console", json={"command": "users"}).json()["output"]
    bad = client.post("/api/admin/console", json={"command": "meta zzzz"}).json()
    assert not bad["ok"] and "не найден" in bad["output"]
    audit = client.post("/api/admin/console", json={"command": "audit 5"}).json()["output"]
    assert "Создан первый администратор" in audit
    r = client.get("/api/admin/logs.zip")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert "attachment" in r.headers["content-disposition"]
