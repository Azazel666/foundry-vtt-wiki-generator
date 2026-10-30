"""The summary cache is the to-do list: whatever has no valid entry is queued again next night."""

import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .storage import read_json, safe_name, write_json, write_text

MAX_FAILED_NIGHTS = 3


def cache_key(kind: str, package_id: str, version: str) -> str:
    return f"{kind}/{package_id}/{version}"


@dataclass
class SourceText:
    text: str
    source: str  # "readme", "repository", "local" or "description"
    url: str = ""


class Cache:
    def __init__(self, state_dir: Path, prompt_version: int) -> None:
        self.root = state_dir / "cache"
        self.prompt_version = prompt_version
        self.failures_path = self.root / "failures.json"

    def _path(self, area: str, kind: str, package_id: str, version: str, suffix: str) -> Path:
        return self.root / area / safe_name(kind) / safe_name(package_id) / f"{safe_name(version)}{suffix}"

    def summary(self, kind: str, package_id: str, version: str) -> dict | None:
        """The entry, if it was made with the current prompt version. The model doesn't matter."""
        entry = read_json(self._path("summaries", kind, package_id, version, ".json"))
        if isinstance(entry, dict) and entry.get("prompt_version") == self.prompt_version:
            return entry
        return None

    def store_summary(self, kind: str, package_id: str, version: str, entry: dict) -> None:
        write_json(self._path("summaries", kind, package_id, version, ".json"),
                   {**entry, "prompt_version": self.prompt_version})

    def source(self, kind: str, package_id: str, version: str) -> SourceText | None:
        path = self._path("sources", kind, package_id, version, ".txt")
        meta = read_json(path.with_suffix(".meta.json"))
        if not isinstance(meta, dict):
            return None
        try:
            return SourceText(path.read_text(encoding="utf-8"), meta.get("source", ""), meta.get("url", ""))
        except OSError:
            return None

    def store_source(self, kind: str, package_id: str, version: str, source: SourceText) -> None:
        path = self._path("sources", kind, package_id, version, ".txt")
        write_text(path, source.text)
        write_json(path.with_suffix(".meta.json"), {"source": source.source, "url": source.url})

    def failures(self) -> dict:
        return read_json(self.failures_path, {})

    def is_blocked(self, kind: str, package_id: str, version: str) -> bool:
        """Failed on MAX_FAILED_NIGHTS nights: leave it alone until the version or the prompt changes."""
        failure = self.failures().get(cache_key(kind, package_id, version))
        return bool(failure and failure.get("prompt_version") == self.prompt_version
                    and failure.get("nights", 0) >= MAX_FAILED_NIGHTS)

    def record_failure(self, kind: str, package_id: str, version: str, error: str, night: date) -> None:
        failures = self.failures()
        key = cache_key(kind, package_id, version)
        entry = failures.get(key, {})
        if entry.get("prompt_version") != self.prompt_version:
            entry = {}
        nights = entry.get("nights", 0) + (entry.get("last_night") != night.isoformat())
        failures[key] = {"attempts": entry.get("attempts", 0) + 1, "nights": nights, "last_error": error,
                         "last_night": night.isoformat(), "prompt_version": self.prompt_version}
        write_json(self.failures_path, failures)

    def clear_failure(self, kind: str, package_id: str, version: str) -> None:
        failures = self.failures()
        if failures.pop(cache_key(kind, package_id, version), None) is not None:
            write_json(self.failures_path, failures)

    def forget(self, kind: str, package_id: str, version: str | None) -> int:
        """Drop cached summaries, sources and failures; returns how many summaries went."""
        removed = 0
        summaries = self.root / "summaries" / safe_name(kind) / safe_name(package_id)
        sources = self.root / "sources" / safe_name(kind) / safe_name(package_id)
        if version is None:
            removed = len(list(summaries.glob("*.json")))
            shutil.rmtree(summaries, ignore_errors=True)
            shutil.rmtree(sources, ignore_errors=True)
        else:
            summary = self._path("summaries", kind, package_id, version, ".json")
            if summary.exists():
                summary.unlink()
                removed = 1
            source = self._path("sources", kind, package_id, version, ".txt")
            source.unlink(missing_ok=True)
            source.with_suffix(".meta.json").unlink(missing_ok=True)
        failures = self.failures()
        prefix = cache_key(kind, package_id, version) if version is not None else f"{kind}/{package_id}/"
        kept = {key: value for key, value in failures.items()
                if not (key == prefix or (version is None and key.startswith(prefix)))}
        if len(kept) != len(failures):
            write_json(self.failures_path, kept)
        return removed

