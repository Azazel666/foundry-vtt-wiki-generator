"""VRAMConductor client: submit to the Redis queue, poll results, read the scheduler's status key."""

import json
import uuid
from typing import Any

import redis

from .config import Config
from .storage import Clock

STATUS_STALE_SECONDS = 120


class ConductorUnreachable(Exception):
    pass


def connect(cfg: Config) -> redis.Redis:
    return redis.Redis(host=cfg.redis_host, port=cfg.redis_port, password=cfg.redis_password,
                       socket_timeout=15, socket_connect_timeout=10, decode_responses=True)


class Conductor:
    def __init__(self, client: redis.Redis, queue: str, clock: Clock) -> None:
        self.client = client
        self.queue = queue
        self.clock = clock

    def status(self) -> dict:
        """The published scheduler status. Missing or not refreshed means the conductor is not there."""
        try:
            raw = self.client.get(f"{self.queue}:status")
        except redis.RedisError as exc:
            raise ConductorUnreachable(f"Redis: {exc}") from exc
        if raw is None:
            raise ConductorUnreachable("status key is missing")
        try:
            status = json.loads(raw)
        except ValueError as exc:
            raise ConductorUnreachable("status key is not JSON") from exc
        if not isinstance(status, dict):
            raise ConductorUnreachable("status key is not an object")
        updated = status.get("updated")
        if not isinstance(updated, (int, float)):
            raise ConductorUnreachable("status key has no update time")
        if self.clock.now().timestamp() - updated > STATUS_STALE_SECONDS:
            raise ConductorUnreachable("status key is stale")
        return status

    def is_idle(self) -> bool:
        """No job of anyone's running or waiting. A hold keeps the GPU from us just the same."""
        status = self.status()
        return not (status.get("running") or status.get("waiting") or status.get("holders"))

    def submit(self, task: dict) -> str:
        task_id = str(uuid.uuid4())
        try:
            self.client.lpush(self.queue, json.dumps({"task_id": task_id, **task}))
        except redis.RedisError as exc:
            raise ConductorUnreachable(f"Redis: {exc}") from exc
        return task_id

    def take_result(self, task_id: str) -> dict | None:
        try:
            raw = self.client.getdel(f"result:{task_id}")
        except redis.RedisError as exc:
            raise ConductorUnreachable(f"Redis: {exc}") from exc
        if raw is None:
            return None
        try:
            envelope = json.loads(raw)
        except ValueError:
            return {"status": "error", "error": "result is not JSON"}
        return envelope if isinstance(envelope, dict) else {"status": "error", "error": "result is not an object"}


def build_task(cfg: Config, schema: dict[str, Any], messages: list[dict]) -> dict:
    return {
        "target": "ollama",
        "endpoint": "/api/chat",
        "priority": cfg.llm_priority,
        "timeout": cfg.task_timeout,
        "payload": {
            "model": cfg.llm_model,
            "think": False,
            "format": schema,
            "options": {"num_ctx": cfg.llm_num_ctx, "temperature": 0.2},
            "messages": messages,
        },
    }
