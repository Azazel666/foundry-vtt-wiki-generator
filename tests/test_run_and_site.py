import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import GOOD_SUMMARY, FakeClock, module, world

from wikigen import publish
from wikigen.cache import Cache
from wikigen.cli import main
from wikigen.config import Config, ConfigError, parse_duration
from wikigen.render import esc, safe_url, slug
from wikigen.run import Runner
from wikigen.storage import read_json


def runner(cfg, clock, conductor, transport):
    return Runner(cfg, clock, conductor.redis, transport)


def live_site(cfg) -> Path:
    return (cfg.state_dir / "site" / "current").resolve()


def page(cfg, *parts) -> str:
    return live_site(cfg).joinpath(*parts, "index.html").read_text(encoding="utf-8")


def report(cfg, clock):
    return read_json(cfg.state_dir / "reports" / f"{clock.now().date().isoformat()}.json")


# Whole runs (12.9)

def test_full_run_publishes_facts_and_summaries(cfg, clock, conductor, transport):
    assert runner(cfg, clock, conductor, transport).run() == 0
    result = report(cfg, clock)
    assert (result["status"], result["ai"]) == ("ok", "completed")
    assert result["envs"] == {"main": "ok", "stage": "ok"}
    html = page(cfg, "modules", "dice")
    assert "Rolls 3D dice" in html and "5.1.2" in html and "5.2.0 ⬆" in html
    assert "player-facing" in html
    assert (cfg.state_dir / "inventory" / "current.json").exists()
    assert not (cfg.state_dir / "run.lock").exists()


def test_gpu_busy_still_publishes_facts(cfg, clock, conductor, transport):
    conductor.running = [{"id": "long"}]
    conductor.publish()
    assert runner(cfg, clock, conductor, transport).run() == 0
    assert report(cfg, clock)["ai"] == "skipped: GPU busy"
    assert conductor.tasks == []
    html = page(cfg, "modules", "dice")
    assert "Summary pending" in html and "5.1.2" in html


def test_strict_skip_publishes_nothing(cfg, clock, conductor, transport):
    conductor.running = [{"id": "long"}]
    conductor.publish()
    strict = replace(cfg, publish_without_ai=False)
    assert runner(strict, clock, conductor, transport).run() == 0
    assert report(cfg, clock)["ai"] == "skipped: GPU busy"
    assert not (cfg.state_dir / "site" / "current").exists()


def test_strict_mode_publishes_when_ai_runs(cfg, clock, conductor, transport):
    strict = replace(cfg, publish_without_ai=False)
    assert runner(strict, clock, conductor, transport).run() == 0
    assert (cfg.state_dir / "site" / "current").exists()


def test_rebuild_every_n_results(cfg, clock, conductor, transport):
    every_two = replace(cfg, rebuild_every=2)
    instance = runner(every_two, clock, conductor, transport)
    builds = []
    original = instance.build_site
    instance.build_site = lambda *args: (builds.append(1), original(*args))
    instance.run()
    assert len(builds) > 3


def test_second_nights_diff_and_changes_page(cfg, clock, conductor, transport, data):
    instance = runner(cfg, clock, conductor, transport)
    instance.run()
    assert not list((cfg.state_dir / "changes").glob("*.json"))
    main_data = data[0]
    module(main_data, "fresh", "1.0", title="Fresh Module")
    clock.sleep(86400)
    instance.run()
    assert report(cfg, clock)["changes"] == 1
    assert "Fresh Module" in page(cfg, "changes")


def test_run_is_idempotent(cfg, clock, conductor, transport):
    instance = runner(cfg, clock, conductor, transport)
    instance.run()
    tasks = len(conductor.tasks)
    instance.run()
    assert len(conductor.tasks) == tasks
    assert report(cfg, clock)["changes"] == 0


def test_failed_run_reports_and_returns_nonzero(cfg, clock, conductor, transport, monkeypatch):
    instance = runner(cfg, clock, conductor, transport)
    monkeypatch.setattr(instance, "collect_and_diff", lambda report: 1 / 0)
    assert instance.run() == 1
    result = report(cfg, clock)
    assert result["status"] == "failed" and "ZeroDivisionError" in result["error"]
    assert not (cfg.state_dir / "run.lock").exists()


def test_build_runs_no_ai(cfg, clock, conductor, transport):
    assert runner(cfg, clock, conductor, transport).build() == 0
    assert conductor.tasks == []
    assert (cfg.state_dir / "site" / "current").exists()


def test_enrich_limit(cfg, clock, conductor, transport):
    assert runner(cfg, clock, conductor, transport).enrich_only(limit=2, ignore_window=True) == 0
    assert len(conductor.tasks) == 2
    assert report(cfg, clock)["envs"] == {"main": "ok", "stage": "ok"}


def test_status_command_data(cfg, clock, conductor, transport):
    instance = runner(cfg, clock, conductor, transport)
    instance.run()
    status = instance.status()
    assert status["last_run"]["ai"] == "completed"
    assert status["coverage"]["pending"] == 0 and status["pending_tasks"] == []


# Lock (12.15)

def test_second_run_exits_without_doing_anything(cfg, clock, conductor, transport):
    cfg.state_dir.mkdir(parents=True)
    (cfg.state_dir / "run.lock").write_text("{}", encoding="utf-8")
    assert runner(cfg, clock, conductor, transport).run() == 0
    assert not (cfg.state_dir / "inventory").exists() and not (cfg.state_dir / "reports").exists()
    assert (cfg.state_dir / "run.lock").exists()


def test_stale_lock_is_taken_over(cfg, clock, conductor, transport):
    cfg.state_dir.mkdir(parents=True)
    lock = cfg.state_dir / "run.lock"
    lock.write_text("{}", encoding="utf-8")
    long_ago = time.time() - 13 * 3600
    os.utime(lock, (long_ago, long_ago))
    assert runner(cfg, clock, conductor, transport).run() == 0
    assert (cfg.state_dir / "reports").exists()


# Rendering (12.13)

EVIL = '<script>alert("x")</script><img src=x onerror=alert(1)>'
EVIL_LINK = "[click](javascript:alert(1))"


@pytest.fixture
def evil_site(cfg, clock, conductor, data, transport):
    main_data = data[0]
    module(main_data, "evil", "1.0", title=EVIL, description=f"<p>{EVIL}</p> {EVIL_LINK}",
           url="javascript:alert(1)", bugs="data:text/html,<script>alert(1)</script>",
           changelog='https://ok.example/"><script>alert(1)</script>',
           authors=[{"name": EVIL, "url": "javascript:alert(2)"}], license=EVIL,
           packs=[{"name": "p", "label": EVIL}])
    world(main_data, "evil-world", "pf2e", {"evil": True}, title=EVIL)
    summary = {**GOOD_SUMMARY, "summary": f"Nice. {EVIL} {EVIL_LINK}", "features": [EVIL], "gm_tips": [EVIL_LINK]}
    conductor.answer = lambda task: {"status": "ok", "body": {"message": {"content": json.dumps(summary)}}}
    assert runner(cfg, clock, conductor, transport).run() == 0
    return live_site(cfg)


def assert_clean(text: str) -> None:
    assert "<script>alert" not in text
    assert 'onerror=alert' not in text.replace("onerror=alert(1)&gt;", "")
    assert "<img src=x" not in text
    assert 'href="javascript:' not in text
    assert 'href="data:' not in text


def test_every_page_escapes_untrusted_text(evil_site):
    pages = list(evil_site.rglob("*.html"))
    assert len(pages) > 10
    for path in pages:
        assert_clean(path.read_text(encoding="utf-8"))


def test_module_page_shows_the_text_harmlessly(evil_site):
    html = (evil_site / "modules" / "evil" / "index.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;" in html or "&#60;script&#62;" in html
    assert "Nice." in html


def test_search_index_is_clean(evil_site):
    index = json.loads((evil_site / "search" / "search_index.json").read_text(encoding="utf-8"))
    assert index["docs"]
    for doc in index["docs"]:
        assert "<script" not in doc["text"].lower() and "<script" not in doc["title"].lower()


def test_only_http_links_are_rendered():
    assert safe_url("https://a.example/x?y=1") == "https://a.example/x?y=1"
    assert safe_url("http://a.example") == "http://a.example"
    assert safe_url("javascript:alert(1)") is None
    assert safe_url("data:text/html,x") is None
    assert safe_url("//a.example") is None
    assert safe_url("README.md") is None
    assert ")" not in safe_url("https://a.example/x)[y](z")
    assert '"' not in safe_url('https://a.example/"><script>')


def test_esc_neutralises_markdown_and_html():
    escaped = esc("a *b* _c_ `d` [e](f) <g> | {h}\nnext")
    assert not set(escaped) & set("*_`[]()<>|{}\n")


def test_slug_defends_paths_and_mkdocs_names():
    assert slug("dice-so-nice") == "dice-so-nice"
    assert slug("JB2A_DnD5e") == "JB2A_DnD5e"
    assert "/" not in slug("../../etc/passwd") and not slug("../../x").startswith(".")
    assert slug("index") != "index" and slug("README").lower() != "readme"


def test_world_descriptions_are_stripped_paragraphs(cfg, clock, conductor, transport, data):
    world(data[0], "story", "pf2e", {}, description="<p>You wake in <em>cryo</em>.</p><p>Alone &amp; afraid.</p>")
    runner(cfg, clock, conductor, transport).build()
    for html in (page(cfg, "worlds", "main", "story"), page(cfg, "worlds", "main")):
        assert "<p>You wake in cryo.</p>" in html
        assert "<p>Alone &amp;" in html or "<p>Alone &#38;" in html


def test_world_descriptions_can_be_hidden(cfg, clock, conductor, transport, data):
    world(data[0], "secret", "pf2e", {}, description="<p>The butler did it</p>")
    hidden = replace(cfg, show_world_descriptions=False)
    runner(hidden, clock, conductor, transport).build()
    assert "butler" not in page(cfg, "worlds", "main", "secret")
    assert "butler" not in page(cfg, "worlds", "main")


def test_enabled_modules_without_a_folder_are_simply_listed(cfg, clock, conductor, transport, data):
    world(data[0], "gap", "pf2e", {"ghost-module": True, "dice": True})
    runner(cfg, clock, conductor, transport).build()
    html = page(cfg, "worlds", "main", "gap")
    assert "ghost" in html and "installed" not in html.split("<article", 1)[1]
    assert "../../../modules/dice/" in html
    assert "2026-10-24" in html


def test_home_is_a_welcome_page_with_the_update_time_in_the_footer(cfg, clock, conductor, transport):
    runner(cfg, clock, conductor, transport).build()
    html = live_site(cfg).joinpath("index.html").read_text(encoding="utf-8")
    article, footer = html.split("<article", 1)[1].split("</article>", 1)
    assert "Welcome to" in article and "Where to start" in article
    assert "worlds/main/" in article and "worlds/stage/" in article
    assert "Summaries:" not in article and "Last updated" not in article
    assert "Last updated" in footer
    assert "Summaries:" not in footer and "AI-generated" not in footer


def test_systems_index_lists_each_system_with_its_summary(cfg, clock, conductor, transport):
    runner(cfg, clock, conductor, transport).run()
    html = page(cfg, "systems")
    assert "<table" not in html.split("<article", 1)[1]
    assert "Rolls 3D dice" in html and "2 worlds" in html
    assert '<span class="wiki-env wiki-env-main">Main 6.5.1</span>' in html
    assert '<span class="wiki-env wiki-env-stage">Stage 6.6.0</span>' in html


def test_envs_have_their_own_badges_everywhere(cfg, clock, conductor, transport):
    runner(cfg, clock, conductor, transport).build()
    for parts in (("modules",), ("modules", "dice"), ("worlds", "main", "strahd"), ("housekeeping", "drift"),
                  ("housekeeping", "unused")):
        assert 'class="wiki-env wiki-env-main"' in page(cfg, *parts)
    assert 'class="wiki-env wiki-env-stage"' in page(cfg, "modules")
    assert 'class="wiki-env wiki-env-main"' in live_site(cfg).joinpath("index.html").read_text(encoding="utf-8")


def test_housekeeping_pages(cfg, clock, conductor, transport, data):
    (data[0] / "modules" / "broken-one").mkdir()
    runner(cfg, clock, conductor, transport).build()
    unused = page(cfg, "housekeeping", "unused")
    article = unused.split("<article", 1)[1]
    candidates, rest = article.split("Removal candidates", 1)[1].split("Libraries nothing needs", 1)
    assert "/unused/" in candidates and "lib-unused" not in candidates
    assert "lib-a" not in candidates and "lib-b" not in candidates
    assert "lib-unused" in rest.split("Unused game systems", 1)[0]
    assert "broken-one" in unused and "DND5E" in unused
    drift = page(cfg, "housekeeping", "drift")
    assert "only here" in drift and "newer" in drift and "missing" in drift


def test_module_page_links_and_relationships(cfg, clock, conductor, transport):
    runner(cfg, clock, conductor, transport).run()
    html = page(cfg, "modules", "dice")
    assert 'href="https://github.com/acme/dice"' in html
    assert "../lib-a/" in html
    lib = page(cfg, "modules", "lib-a")
    assert "../dice/" in lib
    assert "Confidence: high" in html
    index = page(cfg, "modules")
    assert "<h2" in index and "Dice" in index and "Rolls 3D dice." in index


def test_pending_module_shows_the_description(cfg, clock, conductor, transport, data):
    module(data[0], "plain", "1.0", description="<p>A <b>plain</b> module.</p>")
    runner(cfg, clock, conductor, transport).build()
    html = page(cfg, "modules", "plain")
    assert "A plain module." in html and "Summary pending" in html


def test_low_confidence_says_so(cfg, clock, conductor, transport):
    low = {**GOOD_SUMMARY, "confidence": "low"}
    conductor.answer = lambda task: {"status": "ok", "body": {"message": {"content": json.dumps(low)}}}
    runner(cfg, clock, conductor, transport).run()
    assert "brief because the source says little" in page(cfg, "modules", "dice")


def test_reference_version_decides_the_summary(cfg, clock, conductor, transport):
    instance = runner(cfg, clock, conductor, transport)
    instance.run()
    cache = Cache(cfg.state_dir, 1)
    cache.store_summary("module", "dice", "5.2.0", {**cache.summary("module", "dice", "5.2.0"), "summary": "Stage text."})
    instance.build()
    assert "Stage text" not in page(cfg, "modules", "dice")
    cache.store_summary("module", "dice", "5.1.2", {**cache.summary("module", "dice", "5.1.2"), "summary": "Main text."})
    instance.build()
    assert "Main text" in page(cfg, "modules", "dice")


def test_newest_version_is_shown_when_the_reference_lacks_it(cfg, clock, conductor, transport, data):
    module(data[1], "two-sided", "1.0")
    instance = runner(cfg, clock, conductor, transport)
    instance.run()
    cache = Cache(cfg.state_dir, 1)
    cache.store_summary("module", "two-sided", "1.0", {**cache.summary("module", "two-sided", "1.0"), "summary": "Stage only."})
    instance.build()
    assert "Stage only" in page(cfg, "modules", "two-sided")


# Publishing (12.14)

def make_build(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "index.html").write_text(name, encoding="utf-8")
    return path


def test_publish_swaps_the_symlink_and_keeps_three(tmp_path):
    root = tmp_path / "site"
    for number in range(5):
        build = make_build(root, f"build-2026103{number}")
        publish.publish(root, build)
        assert (root / "current" / "index.html").read_text(encoding="utf-8") == build.name
        assert os.readlink(root / "current") == build.name
    assert sorted(path.name for path in root.glob("build-*")) == ["build-20261032", "build-20261033", "build-20261034"]
    assert not list(root.glob(".current-*"))


def test_publish_replaces_in_one_rename(tmp_path, monkeypatch):
    root = tmp_path / "site"
    publish.publish(root, make_build(root, "build-1"))
    calls = []
    original = os.replace
    monkeypatch.setattr(publish.os, "replace", lambda a, b: (calls.append(Path(b).name), original(a, b)))
    publish.publish(root, make_build(root, "build-2"))
    assert calls == ["current"]


# Config

def test_config_defaults(cfg):
    assert cfg.reference == "main" and cfg.envs[0].label == "Main"
    assert cfg.idle_before_start == 600 and cfg.start_window == 7200 and cfg.result_wait == 10800
    assert cfg.llm_model == "qwen3.5:9b" and cfg.deadline.hour == 7


@pytest.mark.parametrize("env, message", [
    ({}, "FOUNDRY_ENVS"),
    ({"FOUNDRY_ENVS": "nope"}, "name=path"),
    ({"FOUNDRY_ENVS": "Main Env=/x"}, "lower-case"),
    ({"FOUNDRY_ENVS": "a=/x,a=/y"}, "twice"),
    ({"FOUNDRY_ENVS": "a=/x"}, "VRAM_REDIS_PASSWORD"),
    ({"FOUNDRY_ENVS": "a=/x", "WIKI_LLM_ENABLED": "false", "WIKI_LLM_PRIORITY": "urgent"}, "WIKI_LLM_PRIORITY"),
    ({"FOUNDRY_ENVS": "a=/x", "WIKI_LLM_ENABLED": "maybe"}, "WIKI_LLM_ENABLED"),
    ({"FOUNDRY_ENVS": "a=/x", "WIKI_LLM_ENABLED": "false", "WIKI_LLM_DEADLINE": "soon"}, "WIKI_LLM_DEADLINE"),
    ({"FOUNDRY_ENVS": "a=/x", "WIKI_LLM_ENABLED": "false", "WIKI_LLM_START_WINDOW": "later"}, "WIKI_LLM_START_WINDOW"),
    ({"FOUNDRY_ENVS": "a=/x", "WIKI_LLM_ENABLED": "false", "WIKI_SCHEDULE": "daily"}, "WIKI_SCHEDULE"),
    ({"FOUNDRY_ENVS": "a=/x", "WIKI_LLM_ENABLED": "false", "TZ": "Mars/Base"}, "TZ"),
])
def test_config_errors_are_clear(env, message):
    with pytest.raises(ConfigError, match=message):
        Config.from_env(env)


def test_envs_as_a_yaml_list_with_separate_roots():
    value = ("- name: main\n  path: /foundry/main\n"
             "- name: stage\n  path: /mnt/other-share/stage/Data\n"
             "- name: canary\n  path: /foundry/canary\n")
    cfg = Config.from_env({"FOUNDRY_ENVS": value, "WIKI_LLM_ENABLED": "false"})
    assert [(env.name, str(env.path)) for env in cfg.envs] == [("main", "/foundry/main"),
                                                               ("stage", "/mnt/other-share/stage/Data"),
                                                               ("canary", "/foundry/canary")]
    assert (cfg.reference, cfg.envs[2].label) == ("main", "Canary")


def test_schedule_can_be_off():
    cfg = Config.from_env({"FOUNDRY_ENVS": "a=/x", "WIKI_LLM_ENABLED": "false", "WIKI_SCHEDULE": "off"})
    assert cfg.schedule == "off"


def test_envs_as_a_one_line_yaml_list():
    cfg = Config.from_env({"FOUNDRY_ENVS": "[{name: main, path: /a}]", "WIKI_LLM_ENABLED": "false"})
    assert cfg.envs[0].name == "main"


@pytest.mark.parametrize("value, message", [
    ("- name: main", "a name and a path"),
    ("- /just/a/path", "a name and a path"),
    ("[]", "non-empty"),
    ("- name: [", "not valid YAML"),
    ("- {name: Main, path: /a}", "lower-case"),
    ("- {name: a, path: /x}\n- {name: a, path: /y}", "twice"),
])
def test_yaml_env_errors_are_clear(value, message):
    with pytest.raises(ConfigError, match=message):
        Config.from_env({"FOUNDRY_ENVS": value, "WIKI_LLM_ENABLED": "false"})


def test_parse_duration():
    assert parse_duration("90s") == 90 and parse_duration("10m") == 600 and parse_duration("2h") == 7200
    assert parse_duration("600") == 600


def test_cli_reports_config_errors(monkeypatch, capsys):
    monkeypatch.delenv("FOUNDRY_ENVS", raising=False)
    assert main(["status"]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_cli_collect_json_writes_nothing(env_vars, monkeypatch, capsys):
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)
    assert main(["collect", "--json"]) == 0
    assert "main" in json.loads(capsys.readouterr().out)["envs"]
    assert not Path(env_vars["WIKI_STATE_DIR"]).exists()


def test_cli_forget(env_vars, monkeypatch, capsys):
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)
    Cache(Path(env_vars["WIKI_STATE_DIR"]), 1).store_summary("module", "dice", "1", {"summary": "x"})
    assert main(["forget", "module", "dice", "1"]) == 0
    assert "Forgot 1" in capsys.readouterr().out


def test_fake_clock_is_used(cfg, conductor):
    assert isinstance(conductor.clock, FakeClock)
