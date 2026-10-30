"""Configuration from the environment. Everything is validated at startup."""

import os
import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd]?)$")
UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
PRIORITIES = ("high", "normal", "background")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Env:
    name: str
    path: Path

    @property
    def label(self) -> str:
        return self.name.capitalize()


@dataclass(frozen=True)
class Config:
    envs: tuple[Env, ...]
    state_dir: Path
    site_url: str | None
    site_name: str
    schedule: str
    run_on_start: bool
    publish_without_ai: bool
    show_world_descriptions: bool
    llm_enabled: bool
    llm_model: str
    llm_num_ctx: int
    llm_priority: str
    prompt_version: int
    source_max_chars: int
    idle_before_start: float
    start_window: float
    deadline: time
    max_inflight: int
    task_timeout: int
    result_wait: float
    rebuild_every: int
    redis_host: str
    redis_port: int
    redis_password: str | None
    queue: str
    github_token: str | None
    timezone: str
    log_level: str

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def reference(self) -> str:
        return self.envs[0].name

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env

        def text(name: str, default: str | None = None) -> str | None:
            value = env.get(name, "").strip()
            return value or default

        cfg = cls(
            envs=parse_envs(text("FOUNDRY_ENVS")),
            state_dir=Path(text("WIKI_STATE_DIR", "/state")),
            site_url=text("WIKI_SITE_URL"),
            site_name=text("WIKI_SITE_NAME", "Foundry VTT Wiki"),
            schedule=text("WIKI_SCHEDULE", "0 1 * * *"),
            run_on_start=_bool(env, "WIKI_RUN_ON_START", False),
            publish_without_ai=_bool(env, "WIKI_PUBLISH_WITHOUT_AI", True),
            show_world_descriptions=_bool(env, "WIKI_SHOW_WORLD_DESCRIPTIONS", True),
            llm_enabled=_bool(env, "WIKI_LLM_ENABLED", True),
            llm_model=text("WIKI_LLM_MODEL", "qwen3.5:9b"),
            llm_num_ctx=_int(env, "WIKI_LLM_NUM_CTX", 16384),
            llm_priority=text("WIKI_LLM_PRIORITY", "normal"),
            prompt_version=_int(env, "WIKI_PROMPT_VERSION", 1),
            source_max_chars=_int(env, "WIKI_SOURCE_MAX_CHARS", 24000),
            idle_before_start=_duration(env, "WIKI_LLM_IDLE_BEFORE_START", "10m"),
            start_window=_duration(env, "WIKI_LLM_START_WINDOW", "2h"),
            deadline=_clock_time(env, "WIKI_LLM_DEADLINE", "07:00"),
            max_inflight=_int(env, "WIKI_LLM_MAX_INFLIGHT", 2),
            task_timeout=_int(env, "WIKI_LLM_TASK_TIMEOUT", 600),
            result_wait=_duration(env, "WIKI_LLM_RESULT_WAIT", "3h"),
            rebuild_every=_int(env, "WIKI_REBUILD_EVERY", 10),
            redis_host=text("REDIS_HOST", "vram-redis"),
            redis_port=_int(env, "REDIS_PORT", 6379),
            redis_password=text("VRAM_REDIS_PASSWORD"),
            queue=text("CONDUCTOR_QUEUE", "vram_conductor_queue"),
            github_token=text("GITHUB_TOKEN"),
            timezone=text("TZ", "Europe/Stockholm"),
            log_level=text("LOG_LEVEL", "INFO").upper(),
        )
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        if self.llm_priority not in PRIORITIES:
            raise ConfigError(f"WIKI_LLM_PRIORITY must be one of {', '.join(PRIORITIES)}, got {self.llm_priority!r}")
        if self.llm_enabled and not self.redis_password:
            raise ConfigError("VRAM_REDIS_PASSWORD is required when WIKI_LLM_ENABLED is true")
        if self.schedule != "off" and len(self.schedule.split()) != 5:
            raise ConfigError(f"WIKI_SCHEDULE must be a five-field cron expression or off, got {self.schedule!r}")
        if self.max_inflight < 1 or self.rebuild_every < 1:
            raise ConfigError("WIKI_LLM_MAX_INFLIGHT and WIKI_REBUILD_EVERY must be at least 1")
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(f"LOG_LEVEL is not a log level: {self.log_level!r}")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"TZ is not a known time zone: {self.timezone!r}") from exc


def parse_envs(value: str | None) -> tuple[Env, ...]:
    """A YAML list of {name, path}, as written in a stack file, or the one-line name=path,name=path."""
    if not value:
        raise ConfigError("FOUNDRY_ENVS is required: a YAML list of name and path, or main=/path,stage=/path")
    pairs = _yaml_envs(value) if value.startswith(("-", "[")) else _inline_envs(value)
    envs: list[Env] = []
    for name, path in pairs:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise ConfigError(f"FOUNDRY_ENVS name must be lower-case letters, digits, '-' or '_', got {name!r}")
        if any(env.name == name for env in envs):
            raise ConfigError(f"FOUNDRY_ENVS lists {name!r} twice")
        envs.append(Env(name, Path(path)))
    return tuple(envs)


def _inline_envs(value: str) -> list[tuple[str, str]]:
    pairs = []
    for part in value.split(","):
        name, separator, path = part.strip().partition("=")
        if not separator or not path.strip():
            raise ConfigError(f"FOUNDRY_ENVS entry must be name=path, got {part.strip()!r}")
        pairs.append((name.strip(), path.strip()))
    return pairs


def _yaml_envs(value: str) -> list[tuple[str, str]]:
    try:
        entries = yaml.safe_load(value)
    except yaml.YAMLError as exc:
        raise ConfigError(f"FOUNDRY_ENVS is not valid YAML: {exc}") from None
    if not isinstance(entries, list) or not entries:
        raise ConfigError("FOUNDRY_ENVS must be a non-empty YAML list")
    pairs = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("path"):
            raise ConfigError(f"FOUNDRY_ENVS entry needs a name and a path, got {entry!r}")
        pairs.append((str(entry["name"]).strip(), str(entry["path"]).strip()))
    return pairs


def parse_duration(value: str) -> float:
    match = DURATION.match(value.strip().lower())
    if not match:
        raise ValueError(f"not a duration: {value!r}")
    return float(match[1]) * UNITS[match[2]]


def _bool(env, name: str, default: bool) -> bool:
    value = env.get(name, "").strip().lower()
    if not value:
        return default
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {value!r}")


def _int(env, name: str, default: int) -> int:
    value = env.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {value!r}") from None


def _duration(env, name: str, default: str) -> float:
    value = env.get(name, "").strip() or default
    try:
        return parse_duration(value)
    except ValueError:
        raise ConfigError(f"{name} must be a duration such as 90s, 10m or 2h, got {value!r}") from None


def _clock_time(env, name: str, default: str) -> time:
    value = env.get(name, "").strip() or default
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise ConfigError(f"{name} must be a local time such as 07:00, got {value!r}") from None
