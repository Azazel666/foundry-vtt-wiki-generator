import json

import plyvel
import pytest
from conftest import FakeClock, module, settings_doc, world, write_json

from wikigen import collect as collect_module
from wikigen.collect import SettingsUnreadable, collect, collect_env, read_enabled
from wikigen.config import Env
from wikigen.models import EnvInventory, World


def test_collects_two_envs(cfg):
    inventory = collect(cfg, FakeClock(), None)
    assert list(inventory.envs) == ["main", "stage"]
    main = inventory.envs["main"]
    assert main.systems["pf2e"].version == "6.5.1"
    assert main.modules["dice"].requires == ["lib-a"]
    assert main.modules["lib-a"].library is True
    assert main.worlds["strahd"].enabled == ["dice"]
    assert main.worlds["strahd"].system == "pf2e"
    assert inventory.envs["stage"].modules["dice"].version == "5.2.0"


def test_malformed_manifests_are_broken_not_fatal(tmp_path):
    root = tmp_path / "Data"
    module(root, "good")
    (root / "modules" / "bad-json").mkdir()
    (root / "modules" / "bad-json" / "module.json").write_text("{nope", encoding="utf-8")
    (root / "modules" / "no-manifest").mkdir()
    write_json(root / "modules" / "no-id" / "module.json", {"title": "x"})
    write_json(root / "modules" / "a-list" / "module.json", [1])
    (root / "modules" / "loose-file.txt").write_text("ignored", encoding="utf-8")
    (root / "worlds").mkdir()
    (root / "worlds" / "README.txt").write_text("ignored", encoding="utf-8")
    (root / "worlds" / "empty-world").mkdir()
    env = collect_env(Env("main", root), None)
    assert list(env.modules) == ["good"]
    assert sorted(b.folder for b in env.broken) == ["a-list", "bad-json", "empty-world", "no-id", "no-manifest"]


def test_dangling_link_is_broken(tmp_path):
    root = tmp_path / "Data"
    module(root, "good")
    (root / "modules" / "linked").symlink_to(tmp_path / "gone")
    env = collect_env(Env("main", root), None)
    assert [(b.folder, b.error) for b in env.broken] == [("linked", "link target is not available")]


def test_system_compatibility_is_kept_per_system(tmp_path):
    root = tmp_path / "Data"
    module(root, "themed", relationships={"systems": [{"id": "pf2e", "compatibility": {"minimum": 7, "verified": "7.9"}},
                                                      {"id": "sf2e"}, {"compatibility": {"minimum": 1}}, "junk"]})
    package = collect_env(Env("main", root), None).modules["themed"]
    assert package.systems == ["pf2e", "sf2e"]
    assert set(package.system_compatibility) == {"pf2e", "sf2e"}
    assert package.system_compatibility["pf2e"].minimum == "7"
    assert package.system_compatibility["pf2e"].verified == "7.9"


def test_odd_manifest_values_are_tolerated(tmp_path):
    root = tmp_path / "Data"
    module(root, "odd", version=2, compatibility={"minimum": 10, "verified": 13.5}, authors="nobody",
           relationships={"requires": ["string", {"id": "lib"}, {"id": "sys", "type": "system"}], "systems": "pf2e"},
           packs=[{"name": "p", "label": "P", "type": "Item", "extra": 1}, "junk"], library="yes")
    package = collect_env(Env("main", root), None).modules["odd"]
    assert package.version == "2"
    assert package.compatibility.minimum == "10" and package.compatibility.verified == "13.5"
    assert package.requires == ["lib"]
    assert package.authors == [] and package.systems == [] and package.library is False
    assert [p.name for p in package.packs] == ["p"]


def test_author_emails_are_not_collected(tmp_path):
    root = tmp_path / "Data"
    module(root, "m", authors=[{"name": "A", "email": "a@example.com", "url": "https://a.example"}])
    dump = collect_env(Env("main", root), None).model_dump_json()
    assert "a@example.com" not in dump and "https://a.example" in dump


def test_missing_env_keeps_previous_inventory(cfg, tmp_path):
    before = collect(cfg, FakeClock(), None)
    missing = Env("main", tmp_path / "gone")
    env = collect_env(missing, before.envs["main"])
    assert env.available is False and "no Foundry data" in env.error
    assert "dice" in env.modules


def test_reads_locked_leveldb_through_a_copy(tmp_path):
    folder = world(tmp_path, "w", enabled={"a": True, "b": False, "c": True})
    settings = folder / "data" / "settings"
    held = plyvel.DB(str(settings))  # holds the LOCK, as Foundry does while the world is loaded
    try:
        held.put(b"!settings!abc", json.dumps(settings_doc({"a": True, "new": True})).encode())
        with pytest.raises(plyvel.IOError):
            plyvel.DB(str(settings))
        assert read_enabled(folder) == ["a", "new"]
        assert (settings / "LOCK").exists()
    finally:
        held.close()


def test_no_setting_means_nothing_enabled(tmp_path):
    assert read_enabled(world(tmp_path, "w")) == []


def test_never_launched_world_has_no_settings(tmp_path):
    folder = tmp_path / "w"
    folder.mkdir()
    assert read_enabled(folder) == []


def test_reads_nedb_settings(tmp_path):
    folder = tmp_path / "w"
    path = folder / "data" / "settings.db"
    path.parent.mkdir(parents=True)
    lines = [
        {"_id": "1", "key": "core.moduleConfiguration", "value": json.dumps({"old": True})},
        {"_id": "2", "key": "core.other", "value": "1"},
        {"_id": "1", "key": "core.moduleConfiguration", "value": json.dumps({"a": True, "b": False})},
        {"_id": "2", "$$deleted": True},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    assert read_enabled(folder) == ["a"]


def _corrupt(folder):
    settings = folder / "data" / "settings"
    for path in settings.iterdir():
        path.unlink()
    (settings / "CURRENT").write_text("MANIFEST-999999\n", encoding="utf-8")


def test_unreadable_settings_retry_then_raise(tmp_path, monkeypatch):
    folder = world(tmp_path, "w", enabled={"a": True})
    _corrupt(folder)
    calls = []
    original = collect_module._copy_database
    monkeypatch.setattr(collect_module, "_copy_database", lambda *args: (calls.append(1), original(*args)))
    with pytest.raises(SettingsUnreadable):
        read_enabled(folder)
    assert len(calls) == 3


def test_half_written_copy_recovers_on_retry(tmp_path, monkeypatch):
    folder = world(tmp_path, "w", enabled={"a": True})
    original = collect_module._copy_database
    calls = []

    def flaky(source, target):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("vanished during copy")
        original(source, target)

    monkeypatch.setattr(collect_module, "_copy_database", flaky)
    assert read_enabled(folder) == ["a"]


def test_stale_world_falls_back_to_previous(tmp_path):
    root = tmp_path / "Data"
    folder = world(root, "w", enabled={"a": True})
    _corrupt(folder)
    previous = EnvInventory(worlds={"w": World(id="w", enabled=["kept"])})
    env = collect_env(Env("main", root), previous)
    assert env.worlds["w"].stale is True
    assert env.worlds["w"].enabled == ["kept"]
    assert collect_env(Env("main", root), None).worlds["w"].enabled == []
