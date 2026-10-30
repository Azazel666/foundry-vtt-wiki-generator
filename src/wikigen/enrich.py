"""Enrich: queue AI summaries for everything missing from the cache, politely.

Nothing is submitted until the GPU has been quiet for a while. At the deadline submitting stops; tasks still
in flight go to pending.json and are collected by the next run.
"""

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from .cache import Cache, SourceText, cache_key
from .conductor import Conductor, ConductorUnreachable, build_task
from .config import Config
from .models import Inventory, Package
from .prompt import Summary, build_messages, schema_for
from .sources import SourceResolver
from .storage import Clock, read_json, write_json

log = logging.getLogger(__name__)

COMPLETED = "completed"
DEADLINE_REACHED = "deadline reached"
SKIPPED_BUSY = "skipped: GPU busy"
SKIPPED_UNREACHABLE = "skipped: conductor unreachable"
DISABLED = "disabled"

GATE_POLL_SECONDS = 30
RESULT_POLL_SECONDS = 2
UNREACHABLE_AFTER_SECONDS = 120
PENDING_MAX_AGE = timedelta(hours=24)


class ModelNotInstalled(Exception):
    pass


class TaskFailed(Exception):
    pass


@dataclass
class Item:
    kind: str
    package: Package
    folder: Path | None
    group: int

    @property
    def key(self) -> str:
        return cache_key(self.kind, self.package.id, self.package.version)


@dataclass
class Work:
    item: Item
    attempt: int = 1


@dataclass
class InFlight:
    work: Work
    source: SourceText
    submitted: datetime


@dataclass
class Outcome:
    status: str
    error: str = ""
    done: int = 0
    failed: int = 0
    deferred: list[str] = field(default_factory=list)


def deadline_after(start: datetime, cfg: Config) -> datetime:
    """The next occurrence of the local deadline, so a run started by hand in the day isn't over at once."""
    deadline = start.replace(hour=cfg.deadline.hour, minute=cfg.deadline.minute, second=0, microsecond=0)
    return deadline if deadline > start else deadline + timedelta(days=1)


def wanted_items(inventory: Inventory, cfg: Config) -> list[Item]:
    """One item per kind/id@version across envs, most useful first, newest-installed first within a group."""
    items: dict[str, Item] = {}
    mtimes: dict[str, float] = {}

    def consider(kind: str, package: Package, env_path: Path, enabled: bool, reference: bool) -> None:
        group = 3 if kind == "system" else 1 if enabled and reference else 2 if enabled else 4
        key = cache_key(kind, package.id, package.version)
        folder = env_path / f"{kind}s" / package.folder if package.folder else None
        if key not in items or group < items[key].group:
            items[key] = Item(kind, package, folder, group)
        mtimes[key] = max(mtimes.get(key, 0.0), package.mtime)

    for env in cfg.envs:
        inventory_env = inventory.envs.get(env.name)
        if inventory_env is None:
            continue
        enabled = {module for world in inventory_env.worlds.values() for module in world.enabled}
        for package in inventory_env.systems.values():
            consider("system", package, env.path, False, env.name == cfg.reference)
        for package in inventory_env.modules.values():
            consider("module", package, env.path, package.id in enabled, env.name == cfg.reference)
    return sorted(items.values(), key=lambda item: (item.group, -mtimes[item.key], item.key))


def parse_result(envelope: dict) -> Summary:
    if envelope.get("status") != "ok":
        if envelope.get("http_status") == 404:
            raise ModelNotInstalled(_reason(envelope))
        raise TaskFailed(_reason(envelope))
    body = envelope.get("body")
    try:
        content = body["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise TaskFailed("result has no message content") from exc
    try:
        return Summary.model_validate_json(content)
    except (ValidationError, ValueError) as exc:
        raise TaskFailed(f"invalid model output: {str(exc)[:300]}") from exc


def _reason(envelope: dict) -> str:
    body = envelope.get("body")
    detail = body.get("error") if isinstance(body, dict) else None
    return str(detail or envelope.get("error") or "task failed")


class Enricher:
    def __init__(self, cfg: Config, cache: Cache, conductor: Conductor | None, resolver: SourceResolver | None,
                 clock: Clock, start: datetime, on_results: Callable[[], None] = lambda: None) -> None:
        self.cfg = cfg
        self.cache = cache
        self.conductor = conductor
        self.resolver = resolver
        self.clock = clock
        self.start = start
        self.night = start.date()
        self.deadline = deadline_after(start, cfg)
        self.on_results = on_results
        self.pending_path = cfg.state_dir / "pending.json"
        self.outcome = Outcome(COMPLETED)

    def run(self, inventory: Inventory, ignore_window: bool = False, limit: int | None = None,
            on_started: Callable[[], None] = lambda: None) -> Outcome:
        try:
            parked = self._collect_pending()
            queue = self._queue(inventory, parked, limit)
            if not queue:
                return self.outcome
            if not ignore_window and not self._wait_for_quiet():
                log.info("ai_skipped reason=gpu_busy")
                self.outcome.status = SKIPPED_BUSY
                return self.outcome
            on_started()
            self.outcome.status = self._work(queue)
        except ConductorUnreachable as exc:
            log.error("ai_skipped reason=conductor_unreachable error=%s", exc)
            self.outcome.status = SKIPPED_UNREACHABLE
            self.outcome.error = str(exc)
        except ModelNotInstalled as exc:
            log.error("ai_skipped reason=model_not_installed model=%s error=%s", self.cfg.llm_model, exc)
            self.outcome.status = SKIPPED_UNREACHABLE
            self.outcome.error = f"model {self.cfg.llm_model} is not installed: {exc}"
        return self.outcome

    def _queue(self, inventory: Inventory, parked: set[str], limit: int | None) -> deque[Work]:
        queue = deque()
        for item in wanted_items(inventory, self.cfg):
            package = item.package
            if item.key in parked or self.cache.summary(item.kind, package.id, package.version) is not None \
                    or self.cache.is_blocked(item.kind, package.id, package.version):
                continue
            if limit is not None and len(queue) >= limit:
                break
            queue.append(Work(item))
        return queue

    def _wait_for_quiet(self) -> bool:
        """True after the required quiet period; False when the window closes first. Any job restarts the timer."""
        window_end = min(self.start + timedelta(seconds=self.cfg.start_window), self.deadline)
        quiet_for = timedelta(seconds=self.cfg.idle_before_start)
        quiet_since = absent_since = None
        while True:
            now = self.clock.now()
            try:
                idle = self.conductor.is_idle()
                absent_since = None
            except ConductorUnreachable:
                idle = False
                absent_since = absent_since or now
                if (now - absent_since).total_seconds() >= UNREACHABLE_AFTER_SECONDS:
                    raise
            quiet_since = (quiet_since or now) if idle else None
            if quiet_since is not None and now - quiet_since >= quiet_for:
                return True
            if now >= window_end:
                return False
            self.clock.sleep(GATE_POLL_SECONDS)

    def _work(self, queue: deque[Work]) -> str:
        inflight: dict[str, InFlight] = {}
        try:
            while queue or inflight:
                if self.clock.now() >= self.deadline:
                    return DEADLINE_REACHED
                self._fill(queue, inflight)
                self._drain(queue, inflight)
                if inflight:
                    self.clock.sleep(RESULT_POLL_SECONDS)
            return COMPLETED
        finally:
            self._park(list(inflight.items()))

    def _fill(self, queue: deque[Work], inflight: dict[str, InFlight]) -> None:
        while queue and len(inflight) < self.cfg.max_inflight and self.clock.now() < self.deadline:
            work = queue.popleft()
            item = work.item
            source = self.resolver.resolve(item.kind, item.package, item.folder)
            if source is None:
                self.outcome.deferred.append(item.key)
                continue
            if not source.text:
                self.cache.record_failure(item.kind, item.package.id, item.package.version,
                                          "no source text", self.night)
                self.outcome.failed += 1
                continue
            task = build_task(self.cfg, schema_for(item.kind), build_messages(item.kind, item.package, source))
            task_id = self.conductor.submit(task)
            log.info("task_submitted key=%s task=%s attempt=%d source=%s", item.key, task_id, work.attempt,
                     source.source)
            inflight[task_id] = InFlight(work, source, self.clock.now())

    def _drain(self, queue: deque[Work], inflight: dict[str, InFlight]) -> None:
        for task_id, job in list(inflight.items()):
            envelope = self.conductor.take_result(task_id)
            if envelope is None:
                if (self.clock.now() - job.submitted).total_seconds() >= self.cfg.result_wait:
                    self._park([(task_id, inflight.pop(task_id))])
                continue
            del inflight[task_id]
            item = job.work.item
            try:
                self._accept(item.kind, item.package.id, item.package.version, envelope, job.source)
            except TaskFailed as exc:
                log.warning("task_failed key=%s attempt=%d error=%s", item.key, job.work.attempt, exc)
                if job.work.attempt == 1:
                    queue.appendleft(Work(item, attempt=2))
                else:
                    self.cache.record_failure(item.kind, item.package.id, item.package.version, str(exc), self.night)
                    self.outcome.failed += 1

    def _accept(self, kind: str, package_id: str, version: str, envelope: dict, source: SourceText | None) -> None:
        summary = parse_result(envelope)
        if kind == "system":
            summary.category = "Game System"
        self.cache.store_summary(kind, package_id, version, {
            **summary.model_dump(),
            "model": self.cfg.llm_model,
            "source": source.source if source else "",
            "source_url": source.url if source else "",
            "created_at": self.clock.now().isoformat(timespec="seconds"),
        })
        self.cache.clear_failure(kind, package_id, version)
        self.outcome.done += 1
        log.info("summary_cached key=%s", cache_key(kind, package_id, version))
        if self.outcome.done % self.cfg.rebuild_every == 0:
            self.on_results()

    def _park(self, jobs: list[tuple[str, InFlight]]) -> None:
        if not jobs:
            return
        pending = read_json(self.pending_path, [])
        for task_id, job in jobs:
            package = job.work.item.package
            log.info("task_parked key=%s task=%s", job.work.item.key, task_id)
            pending.append({"task_id": task_id, "kind": job.work.item.kind, "id": package.id,
                            "version": package.version, "submitted_at": job.submitted.isoformat(timespec="seconds")})
        write_json(self.pending_path, pending)

    def _collect_pending(self) -> set[str]:
        """Pick up what last night left behind. Returns the keys of tasks still out there, not to be queued again."""
        pending = read_json(self.pending_path, [])
        if not pending:
            return set()
        kept, parked = [], set()
        now = self.clock.now()
        for entry in pending:
            key = cache_key(entry["kind"], entry["id"], entry["version"])
            envelope = self.conductor.take_result(entry["task_id"])
            if envelope is None:
                if now - datetime.fromisoformat(entry["submitted_at"]) < PENDING_MAX_AGE:
                    kept.append(entry)
                    parked.add(key)
                continue
            source = self.cache.source(entry["kind"], entry["id"], entry["version"])
            try:
                self._accept(entry["kind"], entry["id"], entry["version"], envelope, source)
            except TaskFailed as exc:
                log.warning("task_failed key=%s attempt=pending error=%s", key, exc)
                self.cache.record_failure(entry["kind"], entry["id"], entry["version"], str(exc), self.night)
        write_json(self.pending_path, kept)
        return parked


def coverage(inventory: Inventory, cfg: Config, cache: Cache) -> dict:
    """How far the to-do list has come: valid summaries, failed-for-good items, and the rest."""
    items = wanted_items(inventory, cfg)
    done = failed = 0
    for item in items:
        package = item.package
        if cache.summary(item.kind, package.id, package.version) is not None:
            done += 1
        elif cache.is_blocked(item.kind, package.id, package.version):
            failed += 1
    return {"total": len(items), "done": done, "failed": failed, "pending": len(items) - done - failed}

