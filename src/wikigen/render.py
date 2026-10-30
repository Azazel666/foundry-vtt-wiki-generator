"""Render the wiki's Markdown and mkdocs config.

The site is public and manifests, READMEs and model output are untrusted, so templates are safe by default:
every `{{ value }}` is escaped unless it is Markup, and Markup only comes from the helpers in this file.
"""

import json
import posixpath
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import yaml
from jinja2 import Environment, PackageLoader, StrictUndefined
from markupsafe import Markup

from . import changes
from .cache import Cache
from .config import Config
from .derive import DriftRow, EnvFindings, compare_versions, drift, env_findings, newest
from .models import EnvInventory, Inventory, Package, World
from .text import html_paragraphs, strip_html

UNCATEGORISED = "Not categorised yet"
ONE_LINE_CHARS = 160
_ENTITIES = {char: f"&#{ord(char)};" for char in "&<>\\`*_{}[]()#|~^=!\"'$"}
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")
_JS_DATE = re.compile(r"^[A-Za-z]{3} ([A-Za-z]{3}) (\d{1,2}) (\d{4})")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
EVENT_LABELS = {
    "system_installed": "system installed", "system_removed": "system removed", "system_updated": "system updated",
    "module_installed": "module installed", "module_removed": "module removed", "module_updated": "module updated",
    "world_created": "world created", "world_removed": "world removed", "module_enabled": "module enabled",
    "module_disabled": "module disabled", "world_system_updated": "world's system updated",
}


def esc(value: object) -> str:
    """Text safe in Markdown and HTML: special characters become numeric entities, line breaks spaces."""
    return "".join(_ENTITIES.get(char, char) for char in " ".join(str(value).split()))


def safe_url(value: str) -> str | None:
    """Only absolute http(s) URLs, with anything that could end the Markdown link percent-encoded."""
    value = value.strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return quote(value, safe=":/?#@!$&*+,;=%-._~")


def slug(identifier: str) -> str:
    """A file name from an id. Foundry ids are URL-safe; the rest is defence, including mkdocs' special names."""
    name = re.sub(r"[^A-Za-z0-9._-]", "-", identifier).strip(".-")
    if not name or name.lower() in ("index", "readme"):
        return f"{name}-page"
    return name


def _finalize(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, Markup):
        return value
    return esc(value)


def _mdjoin(values, separator: str = ", ") -> Markup:
    return Markup(separator.join(value if isinstance(value, Markup) else esc(value) for value in values))


def yaml_title(value: str) -> Markup:
    return Markup(json.dumps(esc(value), ensure_ascii=False))


def played_date(value: str) -> str:
    """lastPlayed is a JavaScript date string, or ISO in places."""
    match = _JS_DATE.match(value)
    if match and match[1] in MONTHS:
        return f"{match[3]}-{MONTHS.index(match[1]) + 1:02d}-{int(match[2]):02d}"
    return value[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", value) else value


def one_line(summary: dict | None, description: str) -> str:
    if summary:
        return _SENTENCE_END.split(summary["summary"].strip(), maxsplit=1)[0]
    text = strip_html(description)
    return text if len(text) <= ONE_LINE_CHARS else f"{text[:ONE_LINE_CHARS].rsplit(' ', 1)[0]}..."


@dataclass
class EnvRow:
    env: str
    label: str
    version: str | None
    marker: str
    worlds: list[World] = field(default_factory=list)


@dataclass
class PackageView:
    kind: str
    id: str
    package: Package  # the version shown: the reference env's, or the newest
    rows: list[EnvRow]
    summary: dict | None
    required_by: list[str] = field(default_factory=list)
    made_for_this: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.package.name

    @property
    def slug(self) -> str:
        return slug(self.id)

    @property
    def path(self) -> str:
        return f"{self.kind}s/{self.slug}.md"

    @property
    def category(self) -> str:
        return self.summary["category"] if self.summary else UNCATEGORISED

    @property
    def line(self) -> str:
        return one_line(self.summary, self.package.description)

    @property
    def description(self) -> str:
        return strip_html(self.package.description)

    @property
    def overview(self) -> str:
        return self.summary["summary"] if self.summary else self.description

    @property
    def world_count(self) -> int:
        return sum(len(row.worlds) for row in self.rows)

    @property
    def brief(self) -> bool:
        return bool(self.summary) and (self.summary.get("source") == "description"
                                       or self.summary.get("confidence") == "low")

class Site:
    def __init__(self, cfg: Config, inventory: Inventory, cache: Cache, footer: str) -> None:
        self.cfg = cfg
        self.inventory = inventory
        self.cache = cache
        self.footer = footer
        self.labels = {env.name: env.label for env in cfg.envs}
        self.envs: dict[str, EnvInventory] = {env.name: inventory.envs.get(env.name, EnvInventory(available=False))
                                              for env in cfg.envs}
        self.modules = self._views("module", "modules")
        self.systems = self._views("system", "systems")
        for view in self.modules.values():
            for other in self.modules.values():
                if view.id in _all_requires(other):
                    view.required_by.append(other.id)
            for system_id in _all_systems(view):
                if system_id in self.systems:
                    self.systems[system_id].made_for_this.append(view.id)
        self.findings: dict[str, EnvFindings] = {name: env_findings(env) for name, env in self.envs.items()}
        self.jinja = Environment(loader=PackageLoader("wikigen", "templates"), finalize=_finalize, autoescape=False,
                                 undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True,
                                 keep_trailing_newline=True)
        self.jinja.filters["mdjoin"] = _mdjoin

    def _views(self, kind: str, section: str) -> dict[str, PackageView]:
        found: dict[str, list[tuple[str, Package]]] = {}
        for name, env in self.envs.items():
            for package_id, package in getattr(env, section).items():
                found.setdefault(package_id, []).append((name, package))
        views = {}
        for package_id, entries in found.items():
            by_env = dict(entries)
            shown = by_env.get(self.cfg.reference) or newest([package for _, package in entries])
            reference = by_env.get(self.cfg.reference)
            rows = []
            for name, env in self.envs.items():
                package = by_env.get(name)
                if kind == "module":
                    worlds = [w for w in env.worlds.values() if package_id in w.enabled]
                else:
                    worlds = [w for w in env.worlds.values() if w.system == package_id]
                rows.append(EnvRow(name, self.labels[name], package.version if package else None,
                                   _marker(reference, package), sorted(worlds, key=lambda w: w.name.lower())))
            views[package_id] = PackageView(kind, package_id, shown, rows,
                                            self.cache.summary(kind, package_id, shown.version))
        return dict(sorted(views.items(), key=lambda item: item[1].title.lower()))

    # Links: all link Markup in the site comes from these.

    def _internal(self, page: str, text: str, target: str) -> Markup:
        relative = posixpath.relpath(target, posixpath.dirname(page) or ".")
        return Markup(f"[{esc(text)}]({quote(relative)})")

    def _external(self, text: str, url: str) -> Markup:
        safe = safe_url(url)
        return Markup(f"[{esc(text)}]({safe})") if safe else Markup(esc(text))

    def _world_path(self, env: str, world_id: str) -> str:
        return f"worlds/{env}/{slug(world_id)}.md"

    def _env_tag(self, env: str, text: str | None = None) -> Markup:
        label = text or self.labels.get(env, env)
        return Markup(f'<span class="wiki-env wiki-env-{slug(env)}">{esc(label)}</span>')

    def _env_link(self, page: str, env: str) -> Markup:
        relative = posixpath.relpath(f"worlds/{env}/index.md", posixpath.dirname(page) or ".")
        return Markup(f"[{self._env_tag(env)}]({quote(relative)})")

    def _env_tags(self, view: PackageView) -> Markup:
        tags = [self._env_tag(row.env, f"{row.label} {row.version or '?'}") for row in view.rows
                if row.version is not None]
        return Markup(" ".join(tags))

    def _helpers(self, page: str) -> dict:
        def package_link(kind: str, package_id: str, title: str | None = None) -> Markup:
            views = self.modules if kind == "module" else self.systems
            view = views.get(package_id)
            if view is None:
                return Markup(esc(title or package_id))
            return self._internal(page, title or view.title, view.path)

        def world_link(env: str, world: World) -> Markup:
            return self._internal(page, world.name, self._world_path(env, world.id))

        return {"module_link": lambda i, title=None: package_link("module", i, title),
                "system_link": lambda i, title=None: package_link("system", i, title),
                "world_link": world_link, "env_link": lambda env: self._env_link(page, env),
                "env_tag": self._env_tag, "env_tags": self._env_tags,
                "external": self._external, "yaml_title": yaml_title, "played": played_date,
                "paragraphs": html_paragraphs,
                "is_http": lambda url: safe_url(url) is not None,
                "event_line": lambda event: self._event_line(page, event)}

    def _event_line(self, page: str, event: dict) -> Markup:
        parts = [self._env_tag(event.get("env", "")),
                 esc(EVENT_LABELS.get(event.get("type", ""), event.get("type", "")))]
        title = event.get("title") or event.get("id")
        if title:
            kind = "system" if event.get("type", "").startswith(("system", "world_system")) else "module"
            views = self.modules if kind == "module" else self.systems
            parts.append(self._internal(page, title, views[event["id"]].path) if event.get("id") in views
                         else esc(title))
        if event.get("from") and event.get("to"):
            parts.append(f"{esc(event['from'])} &rarr; {esc(event['to'])}")
        elif event.get("to") or event.get("from"):
            parts.append(esc(event.get("to") or event.get("from")))
        world = event.get("world")
        if world:
            env = event.get("env", "")
            present = world in self.envs.get(env, EnvInventory()).worlds
            name = event.get("world_title") or world
            parts.append(f"in {self._internal(page, name, self._world_path(env, world)) if present else esc(name)}")
        return Markup(" ".join(parts))

    def _write(self, docs: Path, page: str, template: str, **context) -> None:
        text = self.jinja.get_template(template).render(page=page, cfg=self.cfg, labels=self.labels,
                                                        **self._helpers(page), **context)
        path = docs / page
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def render(self, docs: Path, change_days: list[tuple[date, list[dict]]]) -> None:
        self._write(docs, "index.md", "home.md.j2", env_links=[self._env_link("index.md", name) for name in self.envs])
        self._write(docs, "changes.md", "changes.md.j2", days=change_days)
        self._write(docs, "systems/index.md", "systems_index.md.j2", views=list(self.systems.values()))
        for view in self.systems.values():
            self._write(docs, view.path, "system.md.j2", view=view)
        self._write(docs, "modules/index.md", "modules_index.md.j2", groups=_grouped(self.modules.values()))
        for view in self.modules.values():
            self._write(docs, view.path, "module.md.j2", view=view)
        for name, env in self.envs.items():
            worlds = sorted(env.worlds.values(), key=lambda w: (played_date(w.last_played), w.name), reverse=True)
            self._write(docs, f"worlds/{name}/index.md", "worlds_index.md.j2", env=name, inventory=env, worlds=worlds)
            for world in worlds:
                views = [self.modules[module] for module in world.enabled if module in self.modules]
                unknown = [module for module in world.enabled if module not in self.modules]
                self._write(docs, self._world_path(name, world.id), "world.md.j2", env=name, world=world,
                            groups=_grouped(views), unknown=unknown)
        self._write(docs, "housekeeping/unused.md", "unused.md.j2", envs=self.envs, findings=self.findings)
        rows = drift(self.inventory, self.cfg.reference)
        self._write(docs, "housekeeping/drift.md", "drift.md.j2", rows=rows, reference=self.cfg.reference,
                    cell=_drift_cell)
        assets = Path(__file__).parent / "assets"
        (docs / "assets").mkdir(exist_ok=True)
        (docs / "assets" / "wiki.css").write_text((assets / "wiki.css").read_text(encoding="utf-8"), encoding="utf-8")

    def mkdocs_config(self, docs: Path) -> dict:
        worlds_nav = [{self.labels[name]: f"worlds/{name}/index.md"} for name in self.envs]
        config = {
            "site_name": self.cfg.site_name,
            "docs_dir": str(docs),
            "use_directory_urls": True,
            "copyright": self.footer,
            "theme": {
                "name": "material",
                "font": False,  # no requests to Google Fonts from a public page
                "features": ["navigation.tabs", "navigation.top", "search.suggest", "search.highlight"],
                "palette": [
                    {"media": "(prefers-color-scheme: light)", "scheme": "default",
                     "toggle": {"icon": "material/weather-night", "name": "Switch to dark mode"}},
                    {"media": "(prefers-color-scheme: dark)", "scheme": "slate",
                     "toggle": {"icon": "material/weather-sunny", "name": "Switch to light mode"}},
                ],
            },
            "plugins": ["search"],
            "markdown_extensions": ["tables", "admonition"],
            "extra_css": ["assets/wiki.css"],
            "extra": {"generator": False},
            "not_in_nav": "/modules/*\n/systems/*\n/worlds/*\n",
            "nav": [
                {"Home": "index.md"},
                {"Game Systems": "systems/index.md"},
                {"Modules": "modules/index.md"},
                {"Worlds": worlds_nav},
                {"Housekeeping": [{"Unused": "housekeeping/unused.md"}, {"Drift": "housekeeping/drift.md"}]},
                {"Changes": "changes.md"},
            ],
        }
        if self.cfg.site_url:
            config["site_url"] = self.cfg.site_url
        return config


def _all_requires(view: PackageView) -> set[str]:
    return set(view.package.requires)


def _all_systems(view: PackageView) -> set[str]:
    return set(view.package.systems)


def _marker(reference: Package | None, package: Package | None) -> str:
    if reference is None or package is None or package is reference:
        return ""
    order = compare_versions(package.version, reference.version)
    return "⬆" if order > 0 else "⬇" if order < 0 else ""


def _grouped(views) -> list[tuple[str, list[PackageView]]]:
    groups: dict[str, list[PackageView]] = {}
    for view in views:
        groups.setdefault(view.category, []).append(view)
    return sorted(((name, sorted(items, key=lambda v: v.title.lower())) for name, items in groups.items()),
                  key=lambda group: (group[0] == UNCATEGORISED, group[0]))


def _drift_cell(row: DriftRow, env: str) -> str:
    version = row.versions.get(env)
    state = row.states.get(env)
    if version is None:
        return "—"
    return f"{version} ({state})" if state and state != "same" else version


def write_mkdocs_config(path: Path, config: dict) -> None:
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")


def footer_text(cfg: Config, generated: datetime, status: str) -> str:
    """Raw HTML in the page footer: the parts are escaped here."""
    return (f"Generated {esc(generated.strftime('%Y-%m-%d %H:%M %Z'))}. Facts from the Foundry data; "
            f"summaries are AI-generated. Last run: {esc(status)}")


def load_changes(cfg: Config) -> list[tuple[date, list[dict]]]:
    return changes.load(cfg.state_dir / "changes")
