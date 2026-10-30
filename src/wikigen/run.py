"""The nightly run and its parts, as the CLI calls them."""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import redis

from . import changes, publish
from .cache import Cache
from .collect import collect
from .conductor import Conductor, connect
from .config import Config
from .enrich import DISABLED, SKIPPED_BUSY, SKIPPED_UNREACHABLE, Enricher, Outcome, coverage
from .models import Inventory
from .render import Site, footer_text, load_changes
from .sources import SourceResolver
from .storage import Clock, LockHeld, RunLock, read_json, write_json

log = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 20
USER_AGENT = "foundry-vtt-wiki-generator"
SITE_WAIT_NOTE = "in progress"


@dataclass
class Report:
    started: str
    finished: str = ""
    status: str = "running"
    envs: dict[str, str] = field(default_factory=dict)
    stale_worlds: list[str] = field(default_factory=list)
    changes: int = 0
    summaries: dict = field(default_factory=dict)
    ai: str = ""
    ai_error: str = ""
    error: str = ""

    @property
    def short(self) -> str:
        envs = ", ".join(f"{name} {state}" for name, state in self.envs.items())
        done = self.summaries.get("done_tonight", 0)
        pending = self.summaries.get("pending", 0)
        failed = self.summaries.get("failed", 0)
        ai = self.ai or SITE_WAIT_NOTE
        return (f"{self.started[:16].replace('T', ' ')} to {(self.finished or SITE_WAIT_NOTE)[:16].replace('T', ' ')}. "
                f"Envs: {envs}. Changes: {self.changes}. Summaries: {done} done, {pending} pending, {failed} failed. "
                f"AI: {ai}.")

    def as_dict(self) -> dict:
        return {key: value for key, value in vars(self).items()}


class Runner:
    def __init__(self, cfg: Config, clock: Clock | None = None, redis_client: redis.Redis | None = None,
                 http_transport: httpx.BaseTransport | None = None) -> None:
        self.cfg = cfg
        self.clock = clock or Clock(cfg.tz)
        self.redis_client = redis_client
        self.http_transport = http_transport
        self.state = cfg.state_dir
        self.cache = Cache(cfg.state_dir, cfg.prompt_version)
        self.report: Report | None = None

    # Inventory

    def load_inventory(self, name: str = "current") -> Inventory | None:
        data = read_json(self.state / "inventory" / f"{name}.json")
        if data is None:
            return None
        try:
            return Inventory.model_validate(data)
        except ValueError as exc:
            log.warning("inventory_unreadable name=%s error=%s", name, exc)
            return None

    def collect(self) -> Inventory:
        return collect(self.cfg, self.clock, self.load_inventory())

    def collect_and_diff(self, report: Report) -> Inventory:
        previous = self.load_inventory()
        inventory = collect(self.cfg, self.clock, previous)
        if previous is not None:
            write_json(self.state / "inventory" / "previous.json", previous.model_dump())
        write_json(self.state / "inventory" / "current.json", inventory.model_dump())
        events = changes.diff(previous, inventory)
        changes.record(self.state / "changes", self.clock.now().date(), events)
        report.changes = len(events)
        report.envs = _env_states(inventory)
        report.stale_worlds = [f"{name}/{world.id}" for name, env in inventory.envs.items()
                               for world in env.worlds.values() if world.stale]
        log.info("collected changes=%d envs=%s stale_worlds=%d", len(events), report.envs, len(report.stale_worlds))
        return inventory

    # Site

    def build_site(self, inventory: Inventory, report: Report) -> None:
        report.summaries = {**coverage(inventory, self.cfg, self.cache),
                            "done_tonight": report.summaries.get("done_tonight", 0)}
        now = self.clock.now()
        site = Site(self.cfg, inventory, self.cache, footer_text(self.cfg, now, report.short))
        build_dir = publish.build(site, self.state / "site", now, load_changes(self.cfg))
        publish.publish(self.state / "site", build_dir)

    # Enrichment

    def _enricher(self, http: httpx.Client, start: datetime, on_results) -> Enricher:
        client = self.redis_client or connect(self.cfg)
        conductor = Conductor(client, self.cfg.queue, self.clock)
        resolver = SourceResolver(self.cfg, http, self.cache, self.clock)
        return Enricher(self.cfg, self.cache, conductor, resolver, self.clock, start, on_results)

    def enrich(self, inventory: Inventory, report: Report, start: datetime, ignore_window: bool = False,
               limit: int | None = None, on_started=lambda: None) -> Outcome:
        if not self.cfg.llm_enabled:
            log.info("ai_disabled")
            on_started()
            return Outcome(DISABLED)

        def rebuild() -> None:
            report.summaries["done_tonight"] = enricher.outcome.done
            try:
                self.build_site(inventory, report)
            except Exception:
                log.exception("rebuild_failed")  # the summaries are cached; the final build gets another go

        headers = {"User-Agent": USER_AGENT}
        with httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True, headers=headers,
                          transport=self.http_transport) as http:
            enricher = self._enricher(http, start, rebuild)
            outcome = enricher.run(inventory, ignore_window, limit, on_started)
        report.summaries["done_tonight"] = outcome.done
        return outcome

    # Commands

    def _new_report(self, start: datetime) -> Report:
        self.report = Report(started=start.isoformat(timespec="seconds"))
        return self.report

    def _write_report(self, report: Report) -> None:
        report.finished = self.clock.now().isoformat(timespec="seconds")
        write_json(self.state / "reports" / f"{report.started[:10]}.json", report.as_dict())
        log.info("run_finished status=%s ai=%s changes=%d summaries=%s", report.status, report.ai, report.changes,
                 report.summaries)

    def _locked(self, body) -> int:
        """Runs body under the run lock. A failure ends up in the report and the exit code, never in a crash loop."""
        start = self.clock.now()
        report = self._new_report(start)
        try:
            with RunLock(self.state / "run.lock"):
                try:
                    body(report, start)
                    report.status = "ok"
                except Exception as exc:
                    log.exception("run_failed")
                    report.status = "failed"
                    report.error = f"{type(exc).__name__}: {exc}"
                self._write_report(report)
        except LockHeld:
            log.warning("run_skipped reason=another_run_holds_the_lock")
            return 0
        return 0 if report.status == "ok" else 1

    def run(self) -> int:
        def body(report: Report, start: datetime) -> None:
            inventory = self.collect_and_diff(report)
            published = False

            def publish_facts() -> None:
                nonlocal published
                if not published:
                    published = True
                    self.build_site(inventory, report)

            if self.cfg.publish_without_ai:
                publish_facts()
            outcome = self.enrich(inventory, report, start, on_started=publish_facts)
            report.ai = outcome.status
            report.ai_error = outcome.error
            skipped = outcome.status in (SKIPPED_BUSY, SKIPPED_UNREACHABLE)
            if self.cfg.publish_without_ai or not skipped:
                self._finish_build(inventory, report)
            else:
                log.info("nothing_published reason=ai_skipped")

        return self._locked(body)

    def _finish_build(self, inventory: Inventory, report: Report) -> None:
        report.finished = self.clock.now().isoformat(timespec="seconds")
        self.build_site(inventory, report)

    def build(self) -> int:
        def body(report: Report, _start: datetime) -> None:
            inventory = self.collect_and_diff(report)
            report.ai = "not run"
            self._finish_build(inventory, report)

        return self._locked(body)

    def enrich_only(self, limit: int | None, ignore_window: bool) -> int:
        def body(report: Report, start: datetime) -> None:
            inventory = self.load_inventory() or self.collect_and_diff(report)
            report.envs = _env_states(inventory)
            outcome = self.enrich(inventory, report, start, ignore_window, limit)
            report.ai = outcome.status
            report.ai_error = outcome.error
            if outcome.done:
                self._finish_build(inventory, report)

        return self._locked(body)

    def status(self) -> dict:
        inventory = self.load_inventory()
        reports = sorted((self.state / "reports").glob("*.json")) if (self.state / "reports").is_dir() else []
        return {
            "last_run": read_json(reports[-1]) if reports else None,
            "coverage": coverage(inventory, self.cfg, self.cache) if inventory else None,
            "pending_tasks": read_json(self.state / "pending.json", []),
            "failures": self.cache.failures(),
        }

    def forget(self, kind: str, package_id: str, version: str | None) -> int:
        return self.cache.forget(kind, package_id, version)


def _env_states(inventory: Inventory) -> dict[str, str]:
    return {name: "ok" if env.available else "unavailable" for name, env in inventory.envs.items()}
