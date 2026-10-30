"""The wikigen command."""

import argparse
import json
import logging
import sys

from .config import Config, ConfigError
from .run import Runner


class LineFormatter(logging.Formatter):
    """One JSON object per line, ready for the swarm's log collection."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {"time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
                 "logger": record.name, "message": record.getMessage()}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def _setup_logging(level: str, stream) -> None:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(LineFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wikigen", description="Foundry VTT wiki generator")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="the full nightly run")
    collect = commands.add_parser("collect", help="collect and print the inventory, write nothing")
    collect.add_argument("--json", action="store_true", help="print the whole inventory as JSON")
    commands.add_parser("build", help="collect, diff, build and publish, no AI")
    enrich = commands.add_parser("enrich", help="AI step only")
    enrich.add_argument("--limit", type=int, help="summarise at most this many items")
    enrich.add_argument("--ignore-window", action="store_true", help="skip the idle gate and start at once")
    commands.add_parser("status", help="last run report, cache coverage, pending tasks")
    forget = commands.add_parser("forget", help="drop a cached summary so it is regenerated")
    forget.add_argument("kind", choices=("module", "system"))
    forget.add_argument("id")
    forget.add_argument("version", nargs="?")
    return parser


def _print_collect(runner: Runner, as_json: bool) -> None:
    inventory = runner.collect()
    if as_json:
        print(inventory.model_dump_json(indent=2))
        return
    for name, env in inventory.envs.items():
        state = "" if env.available else f" (unavailable: {env.error})"
        print(f"{name}{state}: {len(env.systems)} systems, {len(env.modules)} modules, "
              f"{len(env.worlds)} worlds, {len(env.broken)} broken")
        for world in env.worlds.values():
            stale = " (stale)" if world.stale else ""
            print(f"  {world.id}: {world.system} {world.system_version}, {len(world.enabled)} enabled{stale}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"wikigen: configuration error: {exc}", file=sys.stderr)
        return 2
    # stdout carries the result for these two, so their logs go to stderr
    _setup_logging(cfg.log_level, sys.stderr if args.command in ("collect", "status") else sys.stdout)
    runner = Runner(cfg)
    if args.command == "run":
        return runner.run()
    if args.command == "collect":
        _print_collect(runner, args.json)
        return 0
    if args.command == "build":
        return runner.build()
    if args.command == "enrich":
        return runner.enrich_only(args.limit, args.ignore_window)
    if args.command == "status":
        print(json.dumps(runner.status(), indent=2, ensure_ascii=False))
        return 0
    removed = runner.forget(args.kind, args.id, args.version)
    print(f"Forgot {removed} cached summaries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
