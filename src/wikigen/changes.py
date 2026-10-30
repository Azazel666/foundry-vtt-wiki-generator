"""Change events: the diff between two inventories, kept per night."""

from datetime import date, timedelta
from pathlib import Path

from .models import EnvInventory, Inventory
from .storage import read_json, write_json

KEEP_DAYS = 365


def diff(previous: Inventory | None, current: Inventory) -> list[dict]:
    """The first run ever is the baseline, and so is the first night of a newly added env."""
    if previous is None:
        return []
    events: list[dict] = []
    for name, env in current.envs.items():
        before = previous.envs.get(name)
        if before is None or not env.available:
            continue
        events += _diff_packages("system", before.systems, env.systems, name)
        events += _diff_packages("module", before.modules, env.modules, name)
        events += _diff_worlds(before, env, name)
    return events


def _diff_packages(kind: str, before: dict, after: dict, env: str) -> list[dict]:
    events = []
    for package_id in sorted(after.keys() - before.keys()):
        events.append({"type": f"{kind}_installed", "env": env, "id": package_id,
                       "title": after[package_id].name, "to": after[package_id].version})
    for package_id in sorted(before.keys() - after.keys()):
        events.append({"type": f"{kind}_removed", "env": env, "id": package_id,
                       "title": before[package_id].name, "from": before[package_id].version})
    for package_id in sorted(before.keys() & after.keys()):
        old, new = before[package_id].version, after[package_id].version
        if old != new:
            events.append({"type": f"{kind}_updated", "env": env, "id": package_id,
                           "title": after[package_id].name, "from": old, "to": new})
    return events


def _diff_worlds(before: EnvInventory, after: EnvInventory, env: str) -> list[dict]:
    events = []
    for world_id in sorted(after.worlds.keys() - before.worlds.keys()):
        events.append({"type": "world_created", "env": env, "world": world_id,
                       "world_title": after.worlds[world_id].name})
    for world_id in sorted(before.worlds.keys() - after.worlds.keys()):
        events.append({"type": "world_removed", "env": env, "world": world_id,
                       "world_title": before.worlds[world_id].name})
    for world_id in sorted(before.worlds.keys() & after.worlds.keys()):
        old, new = before.worlds[world_id], after.worlds[world_id]
        base = {"env": env, "world": world_id, "world_title": new.name}
        for module_id in sorted(set(new.enabled) - set(old.enabled)):
            events.append({"type": "module_enabled", "id": module_id,
                           "title": _module_title(after, module_id), **base})
        for module_id in sorted(set(old.enabled) - set(new.enabled)):
            events.append({"type": "module_disabled", "id": module_id,
                           "title": _module_title(after, module_id, before), **base})
        if old.system_version != new.system_version:
            events.append({"type": "world_system_updated", "id": new.system, "title": _system_title(after, new.system),
                           "from": old.system_version, "to": new.system_version, **base})
    return events


def _module_title(env: EnvInventory, module_id: str, fallback: EnvInventory | None = None) -> str:
    for source in (env, fallback):
        if source is not None and module_id in source.modules:
            return source.modules[module_id].name
    return module_id


def _system_title(env: EnvInventory, system_id: str) -> str:
    package = env.systems.get(system_id)
    return package.name if package else system_id


def record(changes_dir: Path, day: date, events: list[dict]) -> None:
    """Append tonight's events (a hand-started run may add to the same day) and drop expired days."""
    if events:
        path = changes_dir / f"{day.isoformat()}.json"
        write_json(path, read_json(path, []) + events)
    if changes_dir.is_dir():
        cutoff = day - timedelta(days=KEEP_DAYS)
        for path in changes_dir.glob("*.json"):
            try:
                if date.fromisoformat(path.stem) < cutoff:
                    path.unlink()
            except ValueError:
                continue


def load(changes_dir: Path) -> list[tuple[date, list[dict]]]:
    """Every recorded day, newest first."""
    days = []
    if changes_dir.is_dir():
        for path in changes_dir.glob("*.json"):
            try:
                days.append((date.fromisoformat(path.stem), read_json(path, [])))
            except ValueError:
                continue
    return sorted(days, key=lambda entry: entry[0], reverse=True)
