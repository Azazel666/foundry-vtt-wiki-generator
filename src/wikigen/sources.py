"""Find the text to summarise: manifest readme, repository README, shipped README, manifest description."""

import logging
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from .cache import Cache, SourceText
from .config import Config
from .models import Package
from .storage import Clock
from .text import clean_readme, strip_html

log = logging.getLogger(__name__)

MIN_CHARS = 300
FETCH_ATTEMPTS = 3
GITHUB_API = "https://api.github.com"
_GITHUB = re.compile(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/#?]+)", re.I)
_GITHUB_RAW = re.compile(r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/#?]+)", re.I)
_GITHUB_BLOB = re.compile(r"^(https?://)(?:www\.)?github\.com/([^/]+/[^/]+)/blob/(.+)$", re.I)
_GITLAB = re.compile(r"^https?://(?:www\.)?gitlab\.com/([^#?]+)", re.I)
_GITHUB_NON_REPOS = {"orgs", "sponsors", "users", "settings", "marketplace"}
GITLAB_READMES = ("README.md", "readme.md", "README")


def is_http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def github_repo(url: str) -> tuple[str, str] | None:
    match = _GITHUB.match(url) or _GITHUB_RAW.match(url)
    if not match or match[1].lower() in _GITHUB_NON_REPOS:
        return None
    return match[1], re.sub(r"\.git$", "", match[2], flags=re.I)


def gitlab_project(url: str) -> str | None:
    match = _GITLAB.match(url)
    if not match:
        return None
    path = match[1].split("/-/")[0].strip("/")
    path = re.sub(r"\.git$", "", path, flags=re.I)
    return path if path.count("/") >= 1 else None


def raw_readme_url(url: str) -> str | None:
    """The manifest's readme link, as raw text. Some manifests point at a changelog: that says little
    about what a package is for, so the repository README is a better source."""
    if not is_http_url(url):
        return None
    name = urlsplit(url).path.rsplit("/", 1)[-1].lower()
    if "changelog" in name or "changes" in name:
        return None
    blob = _GITHUB_BLOB.match(url)
    if blob:
        return f"{blob[1]}raw.githubusercontent.com/{blob[2]}/{blob[3]}"
    return url


class SourceResolver:
    def __init__(self, cfg: Config, client: httpx.Client, cache: Cache, clock: Clock) -> None:
        self.cfg = cfg
        self.client = client
        self.cache = cache
        self.clock = clock
        self.github_blocked = False

    def resolve(self, kind: str, package: Package, folder: Path | None) -> SourceText | None:
        """None means "not tonight": GitHub is rate limited and nothing better than the description is left."""
        cached = self.cache.source(kind, package.id, package.version)
        if cached is not None:
            return cached
        source, deferred = self._find(package, folder)
        if source is None:
            if deferred:
                return None
            source = SourceText(self._clip(strip_html(package.description)), "description")
        self.cache.store_source(kind, package.id, package.version, source)
        return source

    def _find(self, package: Package, folder: Path | None) -> tuple[SourceText | None, bool]:
        readme_url = raw_readme_url(package.readme)
        if readme_url:
            text = self._text_from(self._get(readme_url))
            if text:
                return SourceText(text, "readme", package.readme), False
        found, deferred = self._from_repository(package)
        if found:
            return found, False
        text = self._local_readme(folder)
        if text:
            return SourceText(text, "local"), False
        return None, deferred

    def _from_repository(self, package: Package) -> tuple[SourceText | None, bool]:
        tried: set[str] = set()
        deferred = False
        for url in (package.url, package.bugs, package.manifest, package.download):
            if not is_http_url(url):
                continue
            repo = github_repo(url)
            project = None if repo else gitlab_project(url)
            target = "/".join(repo) if repo else project
            if not target or target.lower() in tried:
                continue
            tried.add(target.lower())
            if repo:
                if self.github_blocked:
                    deferred = True
                    continue
                found = self._github(*repo)
                deferred = deferred or self.github_blocked
            else:
                found = self._gitlab(project)
            if found:
                return found, False
        return None, deferred

    def _github(self, owner: str, repo: str) -> SourceText | None:
        headers = {"Accept": "application/vnd.github.raw"}
        if self.cfg.github_token:
            headers["Authorization"] = f"Bearer {self.cfg.github_token}"
        response = self._get(f"{GITHUB_API}/repos/{owner}/{repo}/readme", headers)
        if response is not None and response.status_code in (403, 429) \
                and response.headers.get("x-ratelimit-remaining") == "0":
            log.warning("github_rate_limited reset=%s", response.headers.get("x-ratelimit-reset"))
            self.github_blocked = True
            return None
        text = self._text_from(response)
        return SourceText(text, "repository", f"https://github.com/{owner}/{repo}") if text else None

    def _gitlab(self, project: str) -> SourceText | None:
        encoded = quote(project, safe="")
        for name in GITLAB_READMES:
            url = f"https://gitlab.com/api/v4/projects/{encoded}/repository/files/{quote(name, safe='')}/raw?ref=HEAD"
            text = self._text_from(self._get(url))
            if text:
                return SourceText(text, "repository", f"https://gitlab.com/{project}")
        return None

    def _get(self, url: str, headers: dict | None = None) -> httpx.Response | None:
        for attempt in range(FETCH_ATTEMPTS):
            try:
                response = self.client.get(url, headers=headers)
                if response.status_code < 500:
                    return response
                log.debug("fetch_failed url=%s status=%d", url, response.status_code)
            except httpx.HTTPError as exc:
                log.debug("fetch_failed url=%s error=%s", url, exc)
            if attempt + 1 < FETCH_ATTEMPTS:
                self.clock.sleep(2 ** attempt)
        return None

    def _text_from(self, response: httpx.Response | None) -> str | None:
        if response is None or response.status_code != 200:
            return None
        if "text/html" in response.headers.get("content-type", ""):
            return None  # a web page rather than the file: its text would be navigation and noise
        text = clean_readme(response.text)
        return self._clip(text) if len(text) >= MIN_CHARS else None

    def _local_readme(self, folder: Path | None) -> str | None:
        if folder is None or not folder.is_dir():
            return None
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.name.lower() in ("readme.md", "readme.txt", "readme"):
                try:
                    text = clean_readme(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                if len(text) >= MIN_CHARS:
                    return self._clip(text)
        return None

    def _clip(self, text: str) -> str:
        return text[:self.cfg.source_max_chars]
