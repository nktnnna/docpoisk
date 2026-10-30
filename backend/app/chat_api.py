"""Docker-вариант: сервис вопросов-ответов в отдельном контейнере (с логом в общей папке)."""
from . import chat_service, logs

logs.setup("chat")
app = chat_service.app
