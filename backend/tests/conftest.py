"""Тесты не трогают настоящие данные: папки пользователей, логов и файлов — во временном каталоге."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="docsearch-tests-")
os.environ["DATA_DIR"] = _tmp
os.environ["AUTH_DIR"] = os.path.join(_tmp, "auth")
os.environ["LOG_DIR"] = os.path.join(_tmp, "logs")
os.environ["STORAGE_DIR"] = os.path.join(_tmp, "files")
