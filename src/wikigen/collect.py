"""Collect: read manifests and each world's enabled modules. Never writes to the Foundry data."""

import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

import plyvel

from .config import Config, Env
from .models import Author, Broken, Compatibility, EnvInventory, Inventory, Pack, Package, World
from .storage import Clock

log = logging.getLogger(__name__)

SETTING_KEY = "core.moduleConfiguration"
READ_ATTEMPTS = 3
SKIPPED_COPY_FILES = {"LOCK", "LOG", "LOG.old"}


class SettingsUnreadable(Exception):
    pass


def collect(cfg: Config, clock: Clock, previous: Inventory | None) -> Inventory:
    envs = {}
    for env in cfg.envs:
        before = previous.envs.get(env.name) if previous else None
        envs[env.name] = collect_env(env, before)
    return Inventory(generated_at=clock.now().isoformat(timespec="seconds"), envs=envs)


def collect_env(env: Env, previous: EnvInventory | None) -> EnvInventory:
    """A missing data directory fails that env only: its previous inventory is kept and marked unavailable."""
    if not any((env.path / sub).is_dir() for sub in ("systems", "modules", "worlds")):
        error = f"no Foundry data at {env.path}"
        log.error("env_unavailable env=%s error=%s", env.name, error)
        kept = previous.model_copy() if previous else EnvInventory()
        kept.available = False
        kept.error = error
        return kept
    result = EnvInventory()
    _collect_packages(env.path / "systems", "system", result.systems, result.broken)
    _collect_packages(env.path / "modules", "module", result.modules, result.broken)
    _collect_worlds(env.path / "worlds", result, previous)
    log.info("env_collected env=%s systems=%d modules=%d worlds=%d broken=%d", env.name, len(result.systems),
             len(result.modules), len(result.worlds), len(result.broken))
    return result


def _folders(parent: Path) -> list[Path]:
    """Package folders, skipping loose files. Dangling links are kept so they are reported as broken."""
    if not parent.is_dir():
        return []
    return sorted((path for path in parent.iterdir() if path.is_dir() or path.is_symlink()),
                  key=lambda path: path.name.lower())


def _load_manifest(folder: Path, filename: str) -> dict:
    try:
        data = json.loads((folder / filename).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if folder.is_symlink() and not folder.exists():
            raise ValueError("link target is not available") from None
        raise ValueError(f"{filename} is missing") from None
    except OSError as exc:
        raise ValueError(f"{filename} cannot be read: {exc.strerror or exc}") from None
    except ValueError as exc:
        raise ValueError(f"{filename} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{filename} is not a JSON object")
    manifest_id = _text(data.get("id"))
    if not manifest_id:
        raise ValueError(f"{filename} has no id")
    data["id"] = manifest_id
    return data


def _collect_packages(parent: Path, kind: str, into: dict[str, Package], broken: list[Broken]) -> None:
    for folder in _folders(parent):
        try:
            into_package = _package(_load_manifest(folder, f"{kind}.json"), folder)
        except ValueError as exc:
            log.warning("broken_package kind=%s folder=%s error=%s", kind, folder.name, exc)
            broken.append(Broken(kind=kind, folder=folder.name, error=str(exc)))
            continue
        into[into_package.id] = into_package


def _collect_worlds(parent: Path, result: EnvInventory, previous: EnvInventory | None) -> None:
    for folder in _folders(parent):
        try:
            data = _load_manifest(folder, "world.json")
        except ValueError as exc:
            log.warning("broken_world folder=%s error=%s", folder.name, exc)
            result.broken.append(Broken(kind="world", folder=folder.name, error=str(exc)))
            continue
        world = World(id=data["id"], title=_text(data.get("title")), description=_text(data.get("description")),
                      system=_text(data.get("system")), system_version=_text(data.get("systemVersion")),
                      core_version=_text(data.get("coreVersion")), last_played=_text(data.get("lastPlayed")))
        try:
            world.enabled = read_enabled(folder)
        except SettingsUnreadable as exc:
            log.warning("world_stale world=%s error=%s", world.id, exc)
            before = previous.worlds.get(world.id) if previous else None
            world.enabled = list(before.enabled) if before else []
            world.stale = True
        result.worlds[world.id] = world


def _package(data: dict, folder: Path) -> Package:
    relationships = data.get("relationships") if isinstance(data.get("relationships"), dict) else {}
    package_id = data["id"]
    return Package(
        id=package_id,
        title=_text(data.get("title")),
        description=_text(data.get("description")),
        version=_text(data.get("version")),
        authors=[Author(name=_text(a.get("name")), url=_text(a.get("url")))
                 for a in _dicts(data.get("authors"))],
        url=_text(data.get("url")),
        manifest=_text(data.get("manifest")),
        download=_text(data.get("download")),
        bugs=_text(data.get("bugs")),
        readme=_text(data.get("readme")),
        changelog=_text(data.get("changelog")),
        license=_text(data.get("license")),
        compatibility=_compatibility(data.get("compatibility")),
        library=data.get("library") is True,
        systems=[i for i in _relation_ids(relationships.get("systems"), None) if i != package_id],
        system_compatibility={_text(s.get("id")): _compatibility(s.get("compatibility"))
                              for s in _dicts(relationships.get("systems"))
                              if _text(s.get("id")) not in ("", package_id)},
        requires=_relation_ids(relationships.get("requires"), "module"),
        recommends=_relation_ids(relationships.get("recommends"), "module"),
        conflicts=_relation_ids(relationships.get("conflicts"), "module"),
        packs=[Pack(name=_text(p.get("name")), label=_text(p.get("label")), type=_text(p.get("type")))
               for p in _dicts(data.get("packs"))],
        folder=folder.name,
        mtime=_mtime(folder),
    )


def _text(value: Any) -> str:
    """Manifests are hand-written: versions and compatibility are often bare numbers."""
    if isinstance(value, bool) or value is None or isinstance(value, (list, dict)):
        return ""
    return str(value).strip()


def _dicts(value: Any) -> list[dict]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _compatibility(value: Any) -> Compatibility:
    fields = value if isinstance(value, dict) else {}
    return Compatibility(minimum=_text(fields.get("minimum")), verified=_text(fields.get("verified")),
                         maximum=_text(fields.get("maximum")))


def _relation_ids(value: Any, wanted_type: str | None) -> list[str]:
    ids = []
    for entry in _dicts(value):
        identifier = _text(entry.get("id"))
        entry_type = _text(entry.get("type")) or wanted_type
        if identifier and (wanted_type is None or entry_type == wanted_type):
            ids.append(identifier)
    return ids


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def read_enabled(world: Path) -> list[str]:
    leveldb = world / "data" / "settings"
    nedb = world / "data" / "settings.db"
    if leveldb.is_dir():
        return _read_leveldb(leveldb)
    if nedb.is_file():
        return _read_nedb(nedb)
    return []  # never launched: no settings yet


def _read_leveldb(settings: Path) -> list[str]:
    """Foundry holds the database lock while a world is loaded, so read a copy without the LOCK file.
    The copy can catch a half-written log, hence the retries."""
    error: Exception | None = None
    for attempt in range(READ_ATTEMPTS):
        with tempfile.TemporaryDirectory(prefix="wikigen-settings-") as tmp:
            copy = Path(tmp) / "settings"
            try:
                _copy_database(settings, copy)
                return _scan_database(copy)
            except (OSError, ValueError, plyvel.Error) as exc:
                error = exc
                log.debug("settings_read_retry path=%s attempt=%d error=%s", settings, attempt + 1, exc)
    raise SettingsUnreadable(f"{settings}: {error}")


def _copy_database(source: Path, target: Path) -> None:
    target.mkdir()
    for path in source.iterdir():
        if path.name not in SKIPPED_COPY_FILES and path.is_file():
            shutil.copyfile(path, target / path.name)


def _scan_database(path: Path) -> list[str]:
    db = plyvel.DB(str(path), create_if_missing=False, paranoid_checks=False)
    try:
        for _key, raw in db:
            document = _json_or_none(raw)
            if isinstance(document, dict) and document.get("key") == SETTING_KEY:
                return _enabled_ids(document.get("value"))
    finally:
        db.close()
    return []


def _read_nedb(path: Path) -> list[str]:
    """NeDB appends: later lines replace earlier ones with the same _id."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SettingsUnreadable(f"{path}: {exc}") from None
    documents: dict[Any, dict] = {}
    for line in lines:
        document = _json_or_none(line)
        if not isinstance(document, dict):
            continue
        if document.get("$$deleted"):
            documents.pop(document.get("_id"), None)
        else:
            documents[document.get("_id")] = document
    for document in documents.values():
        if document.get("key") == SETTING_KEY:
            try:
                return _enabled_ids(document.get("value"))
            except ValueError as exc:
                raise SettingsUnreadable(f"{path}: {exc}") from None
    return []


def _enabled_ids(value: Any) -> list[str]:
    config = json.loads(value) if isinstance(value, (str, bytes)) else value
    if not isinstance(config, dict):
        raise ValueError(f"{SETTING_KEY} is not an object")
    return sorted(module for module, enabled in config.items() if enabled is True)


def _json_or_none(raw: str | bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None
