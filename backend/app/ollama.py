"""Клиент к Ollama: эмбеддинги и генерация ответа. Всё локально, без интернета."""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from . import config

_TIMEOUT = httpx.Timeout(connect=10, read=600, write=60, pool=10)


async def embed(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for i in range(0, len(texts), config.EMBED_BATCH):
            batch = texts[i : i + config.EMBED_BATCH]
            r = await client.post(
                f"{config.OLLAMA_URL}/api/embed",
                json={"model": config.EMBED_MODEL, "input": batch},
            )
            r.raise_for_status()
            vectors.extend(r.json()["embeddings"])
    return vectors


async def chat_stream(messages: list[dict]) -> AsyncIterator[str]:
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "stream": True,
        "options": {"temperature": config.LLM_TEMPERATURE, "num_ctx": config.LLM_NUM_CTX},
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", f"{config.OLLAMA_URL}/api/chat", json=payload) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode(errors="ignore")
                raise RuntimeError(f"Ollama ответила {r.status_code}: {body}")
            async for line in r.aiter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                piece = msg.get("message", {}).get("content")
                if piece:
                    yield piece
                if msg.get("done"):
                    break


async def chat(messages: list[dict], max_tokens: int = 200, fmt: str | None = None) -> str:
    """Ответ целиком, без потока — для служебных запросов. fmt="json" — модель обязана вернуть JSON."""
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": config.LLM_NUM_CTX, "num_predict": max_tokens},
    }
    if fmt:
        payload["format"] = fmt
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(f"{config.OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "")


async def status() -> dict:
    """Какие модели скачаны — чтобы интерфейс мог подсказать, если что-то не готово."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{config.OLLAMA_URL}/api/tags")
            r.raise_for_status()
            names = {m["name"] for m in r.json().get("models", [])}
    except Exception as e:
        return {"ok": False, "error": f"Ollama недоступна: {e}"}

    def has(model: str) -> bool:
        return model in names or f"{model}:latest" in names

    def info(model: str) -> dict:
        return {"name": model, "ready": has(model), "pull": pull_progress.get(model)}

    return {
        "ok": has(config.LLM_MODEL) and has(config.EMBED_MODEL),
        "llm": info(config.LLM_MODEL),
        "embed": info(config.EMBED_MODEL),
    }


# Прогресс скачивания моделей: {model: "текст статуса"}
pull_progress: dict[str, str] = {}


async def pull(model: str) -> None:
    """Скачивает модель в Ollama (нужен интернет один раз)."""
    pull_progress[model] = "начинаю"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=None, write=60, pool=10)) as client:
            async with client.stream("POST", f"{config.OLLAMA_URL}/api/pull", json={"model": model}) as r:
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    msg = json.loads(line)
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    if msg.get("total"):
                        pct = 100 * msg.get("completed", 0) / msg["total"]
                        pull_progress[model] = f"{pct:.0f}%"
                    else:
                        pull_progress[model] = msg.get("status", "")
        pull_progress[model] = "готово"
    except Exception as e:
        pull_progress[model] = f"ошибка: {e}"
