import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import fakeredis
import httpx
import plyvel
import pytest

from wikigen.config import Config

QUEUE = "vram_conductor_queue"
TZ = ZoneInfo("Europe/Stockholm")
GOOD_SUMMARY = {"category": "Dice", "summary": "Rolls 3D dice. A GM sees them too.", "features": ["3D dice"],
                "gm_tips": ["Enable it in module settings."], "player_facing": True, "confidence": "high"}


class FakeClock:
    """Time only moves when the code sleeps; hooks play the conductor's part at each step."""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 10, 31, 1, 0, tzinfo=TZ)
        self.hooks = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        for hook in self.hooks:
            hook(self.current)


class FakeConductor:
    """Publishes the status key like the real heartbeat, and answers queued tasks when told to."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.running: list[dict] = []
        self.waiting: list[dict] = []
        self.holders: list[str] = []
        self.heartbeat = True
        self.tasks: list[dict] = []  # everything ever received
        self.answer = lambda task: {"status": "ok", "http_status": 200, "error": None,
                                    "body": {"message": {"content": json.dumps(GOOD_SUMMARY)}}}
        self.works = True
        self.schedule = None  # optional hook: called with the time
        clock.hooks.append(self.step)
        self.publish()

    def publish(self) -> None:
        if self.heartbeat:
            status = {"state": "idle", "running": self.running, "waiting": self.waiting, "holders": self.holders,
                      "updated": self.clock.now().timestamp()}
            self.redis.set(f"{QUEUE}:status", json.dumps(status))

    def step(self, now: datetime) -> None:
        if self.schedule:
            self.schedule(now)
        self.publish()
        if not self.works:
            return
        while (raw := self.redis.rpop(QUEUE)) is not None:
            self.deliver(raw)

    def deliver(self, raw: str) -> None:
        task = json.loads(raw)
        self.tasks.append(task)
        self.redis.set(f"result:{task['task_id']}", json.dumps({"task_id": task["task_id"], **self.answer(task)}))

    def queued(self) -> int:
        return self.redis.llen(QUEUE)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def module(root: Path, module_id: str, version: str = "1.0.0", **fields) -> None:
    write_json(root / "modules" / module_id / "module.json",
               {"id": module_id, "title": module_id.title(), "version": version, "description": "<p>Does things.</p>", **fields})


def system(root: Path, system_id: str, version: str = "1.0.0", **fields) -> None:
    write_json(root / "systems" / system_id / "system.json",
               {"id": system_id, "title": system_id.upper(), "version": version, "description": "<p>A system.</p>", **fields})


def settings_doc(enabled: dict) -> dict:
    return {"_id": "abc", "key": "core.moduleConfiguration", "value": json.dumps(enabled)}


def world(root: Path, world_id: str, system_id: str = "pf2e", enabled: dict | None = None, **fields) -> Path:
    folder = root / "worlds" / world_id
    write_json(folder / "world.json", {"id": world_id, "title": world_id.title(), "system": system_id,
                                       "systemVersion": "1.0.0", "coreVersion": "13.351",
                                       "lastPlayed": "Sat Oct 24 2026 22:15:05 GMT+0200 (CEST)", **fields})
    db = plyvel.DB(str(_settings_dir(folder)), create_if_missing=True)
    db.put(b"!settings!other", json.dumps({"_id": "o", "key": "core.other", "value": "1"}).encode())
    if enabled is not None:
        db.put(b"!settings!abc", json.dumps(settings_doc(enabled)).encode())
    db.close()
    return folder


def _settings_dir(folder: Path) -> Path:
    path = folder / "data" / "settings"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def conductor(clock) -> FakeConductor:
    return FakeConductor(clock)


@pytest.fixture
def data(tmp_path) -> tuple[Path, Path]:
    """Two envs. main: a world using dice, a library via requires, an unused module. stage: newer dice."""
    main, stage = tmp_path / "main", tmp_path / "stage"
    system(main, "pf2e", "6.5.1")
    module(main, "dice", "5.1.2", url="https://github.com/acme/dice", relationships={
        "requires": [{"id": "lib-a", "type": "module"}]})
    module(main, "lib-a", "1.0", library=True, relationships={"requires": [{"id": "lib-b"}]})
    module(main, "lib-b", "1.0", library=True)
    module(main, "lib-unused", "1.0", library=True)
    module(main, "unused", "2.0")
    world(main, "strahd", "pf2e", {"dice": True, "unused": False})
    system(stage, "pf2e", "6.6.0")
    system(stage, "dnd5e", "4.0")
    module(stage, "dice", "5.2.0", url="https://github.com/acme/dice")
    module(stage, "stage-only", "0.1")
    world(stage, "strahd-copy", "pf2e", {"dice": True})
    return main, stage


@pytest.fixture
def env_vars(tmp_path, data) -> dict:
    main, stage = data
    return {"FOUNDRY_ENVS": f"main={main},stage={stage}", "WIKI_STATE_DIR": str(tmp_path / "state"),
            "VRAM_REDIS_PASSWORD": "x", "WIKI_REBUILD_EVERY": "1000"}


@pytest.fixture
def cfg(env_vars) -> Config:
    return Config.from_env(env_vars)


README = "This module rolls beautiful three dimensional dice on the canvas for everyone at the table. " * 5


@pytest.fixture
def transport():
    return httpx.MockTransport(lambda request: httpx.Response(200, text=README,
                                                              headers={"content-type": "text/plain"}))
