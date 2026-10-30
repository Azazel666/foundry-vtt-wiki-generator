import json
from dataclasses import replace
from datetime import datetime, timedelta

import httpx
import pytest
from conftest import GOOD_SUMMARY, QUEUE, TZ, FakeClock, FakeConductor

from wikigen.cache import Cache
from wikigen.collect import collect
from wikigen.conductor import Conductor, ConductorUnreachable, build_task
from wikigen.enrich import (COMPLETED, DEADLINE_REACHED, SKIPPED_BUSY, SKIPPED_UNREACHABLE, Enricher, coverage,
                            deadline_after, parse_result, wanted_items)
from wikigen.prompt import SCHEMA, Summary, build_messages
from wikigen.run import Report, Runner
from wikigen.sources import SourceResolver
from wikigen.storage import read_json


def enricher(cfg, clock, conductor, transport, start=None, on_results=lambda: None):
    client = httpx.Client(transport=transport)
    cache = Cache(cfg.state_dir, cfg.prompt_version)
    return Enricher(cfg, cache, Conductor(conductor.redis, QUEUE, clock),
                    SourceResolver(cfg, client, cache, clock), clock, start or clock.now(), on_results)


def inventory(cfg, clock):
    return collect(cfg, clock, None)


def stored(cfg):
    return sorted(p.relative_to(cfg.state_dir / "cache" / "summaries").as_posix()
                  for p in (cfg.state_dir / "cache" / "summaries").rglob("*.json"))


# Conductor client (spec 12.8)

def test_task_shape(cfg, clock, conductor):
    client = Conductor(conductor.redis, QUEUE, clock)
    task_id = client.submit(build_task(cfg, SCHEMA, [{"role": "user", "content": "x"}]))
    task = json.loads(conductor.redis.lindex(QUEUE, 0))
    assert task["task_id"] == task_id
    assert (task["target"], task["endpoint"], task["priority"], task["timeout"]) == ("ollama", "/api/chat", "normal", 600)
    payload = task["payload"]
    assert payload["model"] == "qwen3.5:9b" and payload["think"] is False
    assert payload["options"] == {"num_ctx": 16384, "temperature": 0.2}
    assert payload["format"] == SCHEMA
    assert "stream" not in payload and "keep_alive" not in payload


def test_result_is_taken_once(clock, conductor):
    conductor.redis.set("result:t", json.dumps({"status": "ok"}))
    client = Conductor(conductor.redis, QUEUE, clock)
    assert client.take_result("t") == {"status": "ok"}
    assert client.take_result("t") is None


@pytest.mark.parametrize("field, value, idle", [("running", [{"id": "x"}], False), ("waiting", [{"id": "x"}], False),
                                                ("holders", ["dashboard"], False), ("running", [], True)])
def test_idle_detection(clock, conductor, field, value, idle):
    setattr(conductor, field, value)
    conductor.publish()
    assert Conductor(conductor.redis, QUEUE, clock).is_idle() is idle


def test_missing_status_is_unreachable(clock, conductor):
    conductor.redis.delete(f"{QUEUE}:status")
    with pytest.raises(ConductorUnreachable):
        Conductor(conductor.redis, QUEUE, clock).is_idle()


def test_stale_status_is_unreachable(clock, conductor):
    client = Conductor(conductor.redis, QUEUE, clock)
    assert client.is_idle()
    clock.current += timedelta(seconds=121)
    with pytest.raises(ConductorUnreachable):
        client.is_idle()


def test_max_inflight_is_respected(cfg, clock, conductor, transport):
    conductor.works = False
    high_water = []
    conductor.schedule = lambda now: (high_water.append(conductor.queued()), now >= clock_stop and setattr(conductor, "works", True))
    clock_stop = clock.now() + timedelta(minutes=1)
    worker = enricher(cfg, clock, conductor, transport)
    assert worker.run(inventory(cfg, clock), ignore_window=True).status == COMPLETED
    assert max(high_water) == cfg.max_inflight
    assert stored(cfg)


# Idle gate (12.9)

def test_nothing_is_submitted_until_quiet_for_the_full_period(cfg, clock, conductor, transport):
    start = clock.now()
    first_submit = []
    conductor.answer = lambda task: (first_submit.append(clock.now()), {"status": "ok", "body": {
        "message": {"content": json.dumps(GOOD_SUMMARY)}}})[1]
    assert enricher(cfg, clock, conductor, transport).run(inventory(cfg, clock)).status == COMPLETED
    assert first_submit[0] - start >= timedelta(minutes=10)


def test_a_job_restarts_the_timer(cfg, clock, conductor, transport):
    start = clock.now()
    first_submit = []
    conductor.answer = lambda task: (first_submit.append(clock.now()), {"status": "ok", "body": {
        "message": {"content": json.dumps(GOOD_SUMMARY)}}})[1]

    def busy_at_nine(now):
        conductor.running = [{"id": "chat"}] if timedelta(minutes=9) <= now - start < timedelta(minutes=30) else []

    conductor.schedule = busy_at_nine
    enricher(cfg, clock, conductor, transport).run(inventory(cfg, clock))
    assert first_submit[0] - start >= timedelta(minutes=40)


def test_no_quiet_period_skips_tonight(cfg, clock, conductor, transport):
    conductor.running = [{"id": "long"}]
    conductor.publish()
    started_hook = []
    outcome = enricher(cfg, clock, conductor, transport).run(inventory(cfg, clock), on_started=lambda: started_hook.append(1))
    assert outcome.status == SKIPPED_BUSY
    assert conductor.tasks == [] and conductor.queued() == 0 and not started_hook
    assert clock.now() - datetime(2026, 10, 31, 1, 0, tzinfo=TZ) <= timedelta(hours=2, seconds=30)


def test_conductor_gone_during_the_gate_skips(cfg, clock, conductor, transport):
    conductor.heartbeat = False
    conductor.redis.delete(f"{QUEUE}:status")
    outcome = enricher(cfg, clock, conductor, transport).run(inventory(cfg, clock))
    assert outcome.status == SKIPPED_UNREACHABLE


def test_a_restarting_conductor_is_tolerated(cfg, clock, conductor, transport):
    start = clock.now()

    def flicker(now):
        conductor.heartbeat = not (timedelta(minutes=1) <= now - start < timedelta(minutes=2))
        if not conductor.heartbeat:
            conductor.redis.delete(f"{QUEUE}:status")

    conductor.schedule = flicker
    assert enricher(cfg, clock, conductor, transport).run(inventory(cfg, clock)).status == COMPLETED


# Deadline and pending (12.10)

def test_deadline_stops_submitting_and_parks_inflight(cfg, clock, conductor, transport):
    start = datetime(2026, 10, 31, 6, 59, 50, tzinfo=TZ)
    clock.current = start
    conductor.publish()
    conductor.works = False
    outcome = enricher(cfg, clock, conductor, transport, start).run(inventory(cfg, clock), ignore_window=True)
    assert outcome.status == DEADLINE_REACHED
    pending = read_json(cfg.state_dir / "pending.json")
    assert len(pending) == cfg.max_inflight == conductor.queued()
    assert set(pending[0]) == {"task_id", "kind", "id", "version", "submitted_at"}

    # the next night: the tasks ran, their results are collected and not submitted again
    while (raw := conductor.redis.rpop(QUEUE)) is not None:
        conductor.deliver(raw)
    conductor.works = True
    tonight = datetime(2026, 11, 1, 1, 0, tzinfo=TZ)
    clock.current = tonight
    conductor.publish()
    already = len(conductor.tasks)
    enricher(cfg, clock, conductor, transport, tonight).run(inventory(cfg, clock), ignore_window=True)
    assert read_json(cfg.state_dir / "pending.json") == []
    submitted_again = [t for t in conductor.tasks[already:]]
    all_items = len(wanted_items(inventory(cfg, clock), cfg))
    assert len(submitted_again) == all_items - len(pending)
    assert len(stored(cfg)) == all_items


def test_no_submissions_after_the_deadline(cfg, clock, conductor, transport):
    start = datetime(2026, 10, 31, 7, 0, tzinfo=TZ)
    clock.current = start
    conductor.publish()
    worker = enricher(cfg, clock, conductor, transport, start)
    worker.deadline = start
    assert worker.run(inventory(cfg, clock), ignore_window=True).status == DEADLINE_REACHED
    assert conductor.queued() == 0 and conductor.tasks == []


def test_deadline_is_the_next_occurrence(cfg):
    night = datetime(2026, 10, 31, 1, 0, tzinfo=TZ)
    day = datetime(2026, 10, 31, 14, 0, tzinfo=TZ)
    assert deadline_after(night, cfg) == datetime(2026, 10, 31, 7, 0, tzinfo=TZ)
    assert deadline_after(day, cfg) == datetime(2026, 11, 1, 7, 0, tzinfo=TZ)


def test_old_missing_pending_is_requeued(cfg, clock, conductor, transport):
    from wikigen.storage import write_json
    old = (clock.now() - timedelta(hours=25)).isoformat()
    fresh = clock.now().isoformat()
    write_json(cfg.state_dir / "pending.json", [
        {"task_id": "gone", "kind": "module", "id": "dice", "version": "5.1.2", "submitted_at": old},
        {"task_id": "out", "kind": "module", "id": "unused", "version": "2.0", "submitted_at": fresh},
    ])
    worker = enricher(cfg, clock, conductor, transport)
    worker.run(inventory(cfg, clock), ignore_window=True)
    assert [e["task_id"] for e in read_json(cfg.state_dir / "pending.json")] == ["out"]
    assert "module/dice/5.1.2.json" in stored(cfg)
    assert "module/unused/2.0.json" not in stored(cfg)


def test_result_wait_parks_a_task(cfg, clock, conductor, transport):
    conductor.works = False
    short = replace(cfg, result_wait=60.0)
    outcome = enricher(short, clock, conductor, transport).run(inventory(short, clock), ignore_window=True)
    assert outcome.status == COMPLETED
    assert len(read_json(cfg.state_dir / "pending.json")) == len(wanted_items(inventory(cfg, clock), cfg))


# Failures (12.11)

def bad_answer(task):
    return {"status": "ok", "body": {"message": {"content": "{not json"}}}


def test_invalid_output_is_retried_once_then_recorded(cfg, clock, conductor, transport):
    conductor.answer = bad_answer
    items = len(wanted_items(inventory(cfg, clock), cfg))
    outcome = enricher(cfg, clock, conductor, transport).run(inventory(cfg, clock), ignore_window=True)
    assert outcome.failed == items and outcome.done == 0
    assert len(conductor.tasks) == items * 2
    failure = Cache(cfg.state_dir, 1).failures()["module/dice/5.1.2"]
    assert failure["attempts"] == 1 and failure["nights"] == 1 and "invalid model output" in failure["last_error"]


def test_success_on_the_retry_counts(cfg, clock, conductor, transport):
    answers = iter([bad_answer(None)] + [{"status": "ok", "body": {"message": {"content": json.dumps(GOOD_SUMMARY)}}}] * 99)
    conductor.answer = lambda task: next(answers)
    outcome = enricher(cfg, clock, conductor, transport).run(inventory(cfg, clock), ignore_window=True)
    assert outcome.failed == 0
    assert not Cache(cfg.state_dir, 1).failures()


def test_three_nights_of_failure_stop_until_the_version_changes(cfg, clock, conductor, transport):
    conductor.answer = bad_answer
    for offset in range(3):
        night = datetime(2026, 11, 1 + offset, 1, 0, tzinfo=TZ)
        clock.current = night
        conductor.publish()
        enricher(cfg, clock, conductor, transport, night).run(inventory(cfg, clock), ignore_window=True)
    cache = Cache(cfg.state_dir, 1)
    assert cache.is_blocked("module", "dice", "5.1.2")
    before = len(conductor.tasks)
    night = datetime(2026, 11, 4, 1, 0, tzinfo=TZ)
    clock.current = night
    conductor.publish()
    assert enricher(cfg, clock, conductor, transport, night).run(inventory(cfg, clock), ignore_window=True).status == COMPLETED
    assert len(conductor.tasks) == before
    assert not cache.is_blocked("module", "dice", "5.2.0-new")


def test_two_failures_in_one_night_are_one_night(cfg, clock):
    cache = Cache(cfg.state_dir, 1)
    for _ in range(4):
        cache.record_failure("module", "m", "1", "x", clock.now().date())
    assert cache.failures()["module/m/1"]["nights"] == 1 and not cache.is_blocked("module", "m", "1")


def test_task_error_is_a_failure(cfg, clock, conductor, transport):
    conductor.answer = lambda task: {"status": "error", "http_status": 500, "error": "backend returned HTTP 500", "body": None}
    outcome = enricher(cfg, clock, conductor, transport).run(inventory(cfg, clock), ignore_window=True)
    assert outcome.done == 0 and outcome.failed > 0
    assert "HTTP 500" in Cache(cfg.state_dir, 1).failures()["module/dice/5.1.2"]["last_error"]


def test_model_not_installed_skips_with_a_clear_error(cfg, clock, conductor, transport):
    conductor.answer = lambda task: {"status": "error", "http_status": 404, "error": "backend returned HTTP 404",
                                     "body": {"error": "model 'qwen3.5:9b' not found"}}
    outcome = enricher(cfg, clock, conductor, transport).run(inventory(cfg, clock), ignore_window=True)
    assert outcome.status == SKIPPED_UNREACHABLE
    assert "qwen3.5:9b" in outcome.error and "not installed" in outcome.error
    assert not Cache(cfg.state_dir, 1).failures()


# Cache (12.12)

def fill(cfg, clock, conductor, transport):
    enricher(cfg, clock, conductor, transport).run(inventory(cfg, clock), ignore_window=True)


def test_new_version_regenerates(cfg, clock, conductor, transport):
    fill(cfg, clock, conductor, transport)
    before = len(conductor.tasks)
    fill(cfg, clock, conductor, transport)
    assert len(conductor.tasks) == before
    current = inventory(cfg, clock)
    current.envs["main"].modules["dice"].version = "5.1.3"
    enricher(cfg, clock, conductor, transport).run(current, ignore_window=True)
    assert len(conductor.tasks) == before + 1


def test_prompt_version_regenerates(cfg, clock, conductor, transport):
    fill(cfg, clock, conductor, transport)
    before = len(conductor.tasks)
    fill(replace(cfg, prompt_version=2), clock, conductor, transport)
    assert len(conductor.tasks) == before * 2


def test_model_change_alone_does_not_regenerate(cfg, clock, conductor, transport):
    fill(cfg, clock, conductor, transport)
    before = len(conductor.tasks)
    fill(replace(cfg, llm_model="other:1b"), clock, conductor, transport)
    assert len(conductor.tasks) == before


def test_forget(cfg, clock, conductor, transport):
    fill(cfg, clock, conductor, transport)
    cache = Cache(cfg.state_dir, 1)
    assert cache.forget("module", "dice", "5.1.2") == 1
    assert cache.summary("module", "dice", "5.1.2") is None
    assert cache.summary("module", "dice", "5.2.0") is not None
    assert cache.forget("module", "dice", None) == 1
    assert cache.source("module", "dice", "5.2.0") is None


# Queue and prompt

def test_queue_order(cfg, clock):
    items = wanted_items(inventory(cfg, clock), cfg)
    assert items[0].key == "module/dice/5.1.2"
    groups = [item.group for item in items]
    assert groups == sorted(groups)
    assert ("module/dice/5.2.0", 2) in [(i.key, i.group) for i in items]
    assert {i.key for i in items if i.group == 3} == {"system/pf2e/6.5.1", "system/pf2e/6.6.0", "system/dnd5e/4.0"}
    assert ("module/unused/2.0", 4) in [(i.key, i.group) for i in items]


def test_newest_installed_first_within_a_group(cfg, clock):
    current = inventory(cfg, clock)
    current.envs["main"].modules["unused"].mtime = 100
    current.envs["main"].modules["lib-b"].mtime = 900
    keys = [i.key for i in wanted_items(current, cfg) if i.group == 4]
    assert keys.index("module/lib-b/1.0") < keys.index("module/unused/2.0")


def test_same_version_in_two_envs_is_one_item(cfg, clock):
    current = inventory(cfg, clock)
    current.envs["stage"].modules["dice"].version = "5.1.2"
    keys = [i.key for i in wanted_items(current, cfg)]
    assert keys.count("module/dice/5.1.2") == 1


def test_messages_carry_facts_and_source(cfg, clock):
    from wikigen.cache import SourceText
    package = inventory(cfg, clock).envs["main"].modules["dice"]
    messages = build_messages("module", package, SourceText("Readme words", "repository"))
    assert messages[0]["role"] == "system" and "never" in messages[0]["content"]
    user = messages[1]["content"]
    assert "Id: dice" in user and "Requires: lib-a" in user and user.endswith("Readme words")


def test_modules_cannot_be_categorised_as_game_systems(cfg, clock, conductor, transport):
    fill(cfg, clock, conductor, transport)
    enums = {task["payload"]["format"]["properties"]["category"]["enum"].count("Game System")
             for task in conductor.tasks if "Kind: module" in task["payload"]["messages"][1]["content"]}
    assert enums == {0}
    assert any("Game System" in task["payload"]["format"]["properties"]["category"]["enum"]
               for task in conductor.tasks if "Kind: system" in task["payload"]["messages"][1]["content"])


def test_system_category_is_forced(cfg, clock, conductor, transport):
    fill(cfg, clock, conductor, transport)
    assert Cache(cfg.state_dir, 1).summary("system", "pf2e", "6.5.1")["category"] == "Game System"


def test_summary_schema_rejects_bad_values():
    bad = {**GOOD_SUMMARY, "category": "Nonsense"}
    with pytest.raises(ValueError):
        Summary.model_validate(bad)
    long = Summary.model_validate({**GOOD_SUMMARY, "features": ["x"] * 12, "gm_tips": ["y"] * 9})
    assert len(long.features) == 8 and len(long.gm_tips) == 5


def test_parse_result_needs_message_content():
    with pytest.raises(Exception, match="no message content"):
        parse_result({"status": "ok", "body": "text"})


def test_coverage(cfg, clock, conductor, transport):
    current = inventory(cfg, clock)
    assert coverage(current, cfg, Cache(cfg.state_dir, 1))["done"] == 0
    fill(cfg, clock, conductor, transport)
    result = coverage(current, cfg, Cache(cfg.state_dir, 1))
    assert result["done"] == result["total"] and result["pending"] == 0


def test_llm_disabled_needs_no_redis(cfg, clock):
    runner = Runner(replace(cfg, llm_enabled=False), clock)
    outcome = runner.enrich(inventory(cfg, clock), Report(started="x"), clock.now())
    assert outcome.status == "disabled"


def test_rate_limited_items_stay_pending(cfg, clock, conductor):
    limited = httpx.MockTransport(lambda request: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}))
    outcome = enricher(cfg, clock, conductor, limited).run(inventory(cfg, clock), ignore_window=True)
    assert "module/dice/5.1.2" in outcome.deferred
    assert Cache(cfg.state_dir, 1).summary("module", "dice", "5.1.2") is None


def test_fake_clock_defaults():
    assert FakeClock().now().tzinfo is TZ
    assert FakeConductor(FakeClock()).queued() == 0
