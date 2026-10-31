from datetime import date

import pytest
from conftest import FakeClock

from wikigen import changes
from wikigen.collect import collect
from wikigen.derive import (DEPRECATED, TOO_NEW, UNVERIFIED, compare_versions, compat_issues, compat_level, drift,
                            env_findings, foundry_version, requirement_closure)
from wikigen.models import Compatibility, EnvInventory, Inventory, Package, World


def test_removal_candidates_exclude_transitive_requirements(cfg):
    env = collect(cfg, FakeClock(), None).envs["main"]
    assert requirement_closure(["dice"], env.modules) == {"lib-a", "lib-b"}
    findings = env_findings(env)
    assert findings.removal_candidates == ["unused"]
    assert findings.unused_libraries == ["lib-unused"]


def test_modules_a_worlds_system_requires_are_in_use():
    env = EnvInventory(systems={"sys": Package(id="sys", requires=["needed"]), "other": Package(id="other", requires=["spare"])},
                       modules={"needed": Package(id="needed", requires=["deep"]), "deep": Package(id="deep"),
                                "spare": Package(id="spare")},
                       worlds={"w": World(id="w", system="sys")})
    assert env_findings(env).removal_candidates == ["spare"]


def test_requirement_cycles_terminate():
    modules = {"a": Package(id="a", requires=["b"]), "b": Package(id="b", requires=["a"])}
    assert requirement_closure(["a"], modules) == {"a", "b"}


def test_unused_systems(cfg):
    stage = collect(cfg, FakeClock(), None).envs["stage"]
    assert env_findings(stage).unused_systems == ["dnd5e"]


def test_drift_states(cfg):
    inventory = collect(cfg, FakeClock(), None)
    inventory.envs["stage"].modules["lib-a"] = inventory.envs["main"].modules["lib-a"]
    inventory.envs["main"].modules["older"] = Package(id="older", version="2.0")
    inventory.envs["stage"].modules["older"] = Package(id="older", version="1.0")
    rows = {row.id: row for row in drift(inventory, "main")}
    assert rows["dice"].states == {"stage": "newer"}
    assert rows["older"].states == {"stage": "older"}
    assert rows["unused"].states == {"stage": "missing"}
    assert rows["stage-only"].states == {"stage": "only here"}
    assert rows["dnd5e"].kind == "system"
    assert "lib-a" not in rows


@pytest.mark.parametrize("left, right, expected", [
    ("1.2.0", "1.10.0", -1),
    ("2.0", "2.0.0", 0),
    ("v1.2", "1.1", 1),
    ("1.0.0-beta.2", "1.0.0", -1),
    ("banana", "1.0", 1),
    ("", "1.0", -1),
    ("release-7", "release-12", -1),
    ("13.351", "13.351", 0),
])
def test_compare_versions_never_crashes(left, right, expected):
    assert compare_versions(left, right) == expected
    assert compare_versions(right, left) == -expected


@pytest.mark.parametrize("compatibility, running, expected", [
    (dict(minimum="10", verified="12", maximum="12"), "13.351", DEPRECATED),
    (dict(maximum="13.300"), "13.351", DEPRECATED),
    (dict(minimum="10", verified="12"), "13.351", UNVERIFIED),
    (dict(minimum="14"), "13.351", TOO_NEW),
    (dict(minimum="13.348", verified="13.351", maximum="13"), "13.351", None),
    (dict(minimum="13", verified="13.341"), "13.351", None),
    (dict(verified="14"), "13.351", None),
    (dict(verified="soon"), "13.351", None),
    (dict(minimum="5.3.2", verified="7.0.5"), "7.10.1", None),
    (dict(minimum="5.3.2", verified="7.0.5"), "8.4.0", UNVERIFIED),
    (dict(), "13.351", None),
])
def test_compat_level(compatibility, running, expected):
    assert compat_level(Compatibility(**compatibility), running) == expected


def test_foundry_version_is_the_newest_any_world_was_opened_with():
    worlds = {"a": World(id="a", core_version="13.9"), "b": World(id="b", core_version="13.351"), "c": World(id="c")}
    assert foundry_version(EnvInventory(worlds=worlds)) == "13.351"
    assert foundry_version(EnvInventory()) == ""


def test_compat_issues_check_foundry_and_installed_systems():
    old = Compatibility(minimum="10", verified="12", maximum="12")
    env = EnvInventory(
        systems={"pf2e": Package(id="pf2e", title="PF2e", version="8.4.0", compatibility=Compatibility(verified="13"))},
        modules={"old": Package(id="old", title="Old", version="1", compatibility=old),
                 "fine": Package(id="fine", compatibility=Compatibility(verified="13")),
                 "themed": Package(id="themed", system_compatibility={"pf2e": Compatibility(verified="7.0.5"),
                                                                      "sf2e": Compatibility(maximum="1")})},
        worlds={"w": World(id="w", system="pf2e", core_version="13.351", enabled=["old"]),
                "x": World(id="x", system="pf2e", enabled=["old"])})
    found = {(i.id, i.level, i.target, i.running, i.worlds) for i in compat_issues(env)}
    assert found == {("old", DEPRECATED, "Foundry", "13.351", 2), ("themed", UNVERIFIED, "PF2e", "8.4.0", 0)}


def test_without_a_foundry_version_only_systems_are_checked():
    env = EnvInventory(modules={"old": Package(id="old", compatibility=Compatibility(maximum="12"))})
    assert compat_issues(env) == []


def inventory_of(**env_fields) -> Inventory:
    return Inventory(generated_at="t", envs={"main": EnvInventory(**env_fields)})


def test_first_run_is_the_baseline(cfg):
    assert changes.diff(None, collect(cfg, FakeClock(), None)) == []


def test_new_env_is_a_baseline():
    previous = Inventory(generated_at="t", envs={})
    assert changes.diff(previous, inventory_of(modules={"a": Package(id="a")})) == []


def test_diff_events():
    before = inventory_of(
        systems={"gone-sys": Package(id="gone-sys", version="1"), "pf2e": Package(id="pf2e", version="1")},
        modules={"up": Package(id="up", version="1"), "gone": Package(id="gone", version="1"),
                 "m": Package(id="m", title="Em")},
        worlds={"w": World(id="w", enabled=["m", "off"], system="pf2e", system_version="1"), "old": World(id="old")})
    after = inventory_of(
        systems={"new-sys": Package(id="new-sys", version="2"), "pf2e": Package(id="pf2e", version="2")},
        modules={"up": Package(id="up", version="2"), "new": Package(id="new", version="1"),
                 "m": Package(id="m", title="Em")},
        worlds={"w": World(id="w", enabled=["m", "on"], system="pf2e", system_version="2"), "fresh": World(id="fresh")})
    events = changes.diff(before, after)
    found = {(e["type"], e.get("id") or e.get("world")) for e in events}
    assert found == {
        ("system_installed", "new-sys"), ("system_removed", "gone-sys"), ("system_updated", "pf2e"),
        ("module_installed", "new"), ("module_removed", "gone"), ("module_updated", "up"),
        ("world_created", "fresh"), ("world_removed", "old"), ("module_enabled", "on"),
        ("module_disabled", "off"), ("world_system_updated", "pf2e"),
    }
    updated = next(e for e in events if e["type"] == "module_updated")
    assert (updated["env"], updated["from"], updated["to"]) == ("main", "1", "2")
    assert next(e for e in events if e["type"] == "module_enabled")["world"] == "w"


def test_unavailable_env_produces_no_events():
    before = inventory_of(modules={"a": Package(id="a")})
    after = inventory_of(available=False)
    assert changes.diff(before, after) == []


def test_records_per_day_and_prunes(tmp_path):
    folder = tmp_path / "changes"
    changes.record(folder, date(2025, 1, 1), [{"type": "a"}])
    changes.record(folder, date(2026, 10, 30), [])
    assert not (folder / "2026-10-30.json").exists()
    assert (folder / "2025-01-01.json").exists() is False
    changes.record(folder, date(2026, 10, 31), [{"type": "a"}])
    changes.record(folder, date(2026, 10, 31), [{"type": "b"}])
    assert [day.isoformat() for day, _ in changes.load(folder)] == ["2026-10-31"]
    assert [e["type"] for e in changes.load(folder)[0][1]] == ["a", "b"]
