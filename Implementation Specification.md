# Foundry VTT Wiki Generator: Implementation Specification

A nightly job that inventories every Foundry VTT environment in the Borg-Collective swarm and publishes a
static, public wiki for our GMs: which game systems, modules and worlds exist, what each module is for, which
world uses what, and what could be removed.

The facts (installed, versions, enabled in which world) come from Foundry's own files and are always exact.
An LLM is used for one thing only: turning a module's or system's README into a short, GM-facing summary.

---

## 1. Goals

- **Nobody maintains it.** Install a module or enable it in a world today; it is on the wiki tomorrow.
- **For GMs.** "What can I use at the table in this campaign, and how?"
- **One page per system and per module,** tagged with the version installed in each environment. No
  duplicate pages per environment.
- **Worlds per environment,** each listing its game system and the modules it has enabled.
- **Housekeeping:** modules and systems nothing uses, compatibility problems, and version drift between
  environments.
- **Public, static, English, read-only.** No human editing, no auth.

Non-goals (v1): scanning world documents for actual use of a module (enabled = used), summarising
changelogs, editing in the browser, auth.

---

## 2. Environment

| | |
|---|---|
| Environments | `main` (weekly play), `stage` (testing), later `canary` (next major version). All in Docker Swarm |
| Foundry image | `felddy/foundryvtt`, v13 at the time of writing. Data on shared NFS at `${DATA_PATH}/foundry/<env>/data/Data` |
| Worlds | Normally **loaded around the clock** on main and stage, so their databases are locked (see §5.3) |
| LLM | Ollama on a Tesla P100 16 GB, reachable **only through VRAMConductor** (≥ 1.2.1). Model `qwen3.5:9b` |
| Publishing | `https://wiki.vtt.<domain>` through Traefik (TLS by Traefik) |
| Deploy | Stack lives in the `borg-collective-swarm` repo; this repo builds and pushes the image to `registry.joraas.systems/foundry-vtt-wiki-generator:<tag>` |

---

## 3. Architecture

Two containers in one stack:

```
                       read-only                        borg-genai
 /foundry (NFS) ───────────────▶ ┌──────────────┐ ── LPUSH / GETDEL / GET status ──▶ vram-redis ──▶ VRAMConductor ──▶ Ollama
                                 │  generator   │
 GitHub / GitLab  ◀── HTTPS ──── │ (supercronic)│
                                 └──────┬───────┘
                                        │ writes
                                        ▼
                         /state (NFS): cache, inventory, site/
                                        │ read-only
                                        ▼
                                 ┌──────────────┐
                                 │    nginx     │ ◀── Traefik ◀── wiki.vtt.<domain>
                                 └──────────────┘
```

- **generator**: Python app. Runs the nightly job from an internal cron (supercronic). Idle otherwise.
- **nginx**: serves the built static site from `/state/site/current`. Stock `nginx:alpine` image plus a small
  config; it does not need to be part of this image (the swarm repo can mount the config).

### Tech stack

- Python 3.12, `python:3.12-slim` base
- `plyvel` (LevelDB, has manylinux wheels), `httpx`, `redis`, `pydantic` v2, `jinja2`, `markdown-it-py` or
  similar for README cleaning
- `mkdocs` + `mkdocs-material` to build the site
- `supercronic` for the schedule
- No database. All state is JSON files on NFS (SQLite on NFS has locking problems in this cluster)

---

## 4. Nightly run

Default schedule `0 1 * * *` (01:00, `TZ=Europe/Stockholm`).

```
01:00  1. Collect     every env: systems, modules, worlds, enabled modules per world   (seconds, no GPU)
       2. Diff        against the previous inventory → change events
       3. Build       render Markdown → mkdocs build → publish                         (facts are now live)
       4. Pending     collect results of tasks left over from the previous night (GETDEL)
       5. Enrich      queue AI summaries for everything missing from the cache
           ├─ idle gate: watch the conductor until it has had no running or waiting jobs for
           │     WIKI_LLM_IDLE_BEFORE_START (10 min). Nothing is submitted before that.
           │     no such quiet period within WIKI_LLM_START_WINDOW (2 h from run start)
           │     → log "GPU busy, AI skipped tonight", go to 6
           ├─ quiet → keep at most WIKI_LLM_MAX_INFLIGHT tasks queued, cache each result,
           │     rebuild + publish every WIKI_REBUILD_EVERY results
           └─ stop submitting at WIKI_LLM_DEADLINE (07:00); in-flight task ids → pending.json
       6. Build       final rebuild + publish, write run report, exit
```

**"Skip and postpone" applies to the AI part only.** Steps 1–3 use no GPU, so the wiki's facts (new modules,
versions, worlds) are always updated; anything without a summary shows *Summary pending*. Whatever is not
summarised tonight is picked up next night, because the cache is the to-do list. Setting
`WIKI_PUBLISH_WITHOUT_AI=false` makes a skipped night publish nothing at all instead (strict skip).

The run must be idempotent and safe to start by hand at any time (`wikigen run`). Only one run at a time:
take a lock file in `/state` (with a stale-lock timeout) and exit if it's held.

---

## 5. Collecting

### 5.1 Configuration of environments

```yaml
# In the stack's environment block. Order = display order. The first one is the drift reference.
FOUNDRY_ENVS: |
  - name: main
    path: /foundry/main
  - name: stage
    path: /foundry/stage
  - name: canary
    path: /foundry/canary
```

Each path is a Foundry `Data` directory containing `systems/`, `modules/`, `worlds/`, as seen inside the
container. The environments share no root folder on NFS, so the stack mounts each one read-only on its own.
Adding an env takes one list entry and one mount; see the README. Display label =
capitalised name (`main` → `Main`). A path that doesn't exist is an error for that env only: log it, mark the
env *unavailable* in the run report, keep its previous inventory, continue with the others.

### 5.2 Manifests

| Kind | File |
|---|---|
| System | `systems/<id>/system.json` |
| Module | `modules/<id>/module.json` |
| World | `worlds/<id>/world.json` |

Fields to read (all optional except `id`, tolerate missing/extra fields and malformed files):

- **System / module:** `id`, `title`, `description` (HTML), `version`, `authors[]` (`name`, `url`), `url`,
  `manifest`, `download`, `bugs`, `readme`, `changelog`, `license`, `compatibility` (`minimum`, `verified`,
  `maximum`), `relationships` (`systems[]` with each entry's `compatibility`, `requires[]`, `recommends[]`,
  `conflicts[]`), `library` (bool), `packs[]` (names/labels/types only).
- **World:** `id`, `title`, `description` (HTML), `system`, `systemVersion`, `coreVersion`, `lastPlayed`.

A folder without a readable manifest: log a warning, list it on the housekeeping page as *broken*.

### 5.3 Enabled modules per world

Since Foundry v11, world data is LevelDB, one database per collection under `worlds/<id>/data/<collection>/`.
The enabled modules are the world setting `core.moduleConfiguration` in `worlds/<id>/data/settings/`.

Foundry holds the database lock while the world is loaded, which is nearly always. So:

1. Copy `worlds/<id>/data/settings/` to a temp dir, **without the `LOCK` file**.
2. Open the copy with `plyvel` (`create_if_missing=False`, `paranoid_checks=False`) and iterate all entries.
3. Parse each value as JSON; find the document whose `key == "core.moduleConfiguration"`. Don't depend on the
   LevelDB key format (`!settings!<id>`). Its `value` is a JSON **string** of `{moduleId: bool}`; decode it.
4. Enabled modules = ids with `true`.
5. Delete the copy.

The copy can catch a half-written log file. If opening or reading fails, retry the copy up to 3 times, then
fall back to that world's result from the previous inventory and mark it *stale* (shown on the world page).

Fallback for pre-v11 worlds: if `worlds/<id>/data/settings.db` (NeDB, one JSON document per line) exists and
the LevelDB dir doesn't, read that instead.

Never write to `/foundry`. The mount is read-only anyway; the code must not try.

### 5.4 Derived data, per environment

- **World → system** from `world.json`.
- **Enabled but not installed:** enabled ids with no module folder. Shown as a warning on the world page.
- **Requirement closure:** for each world, the transitive `relationships.requires` of its enabled modules.
- **Removal candidates** (modules): installed, enabled in no world of that env, and not in the requirement
  closure of any world. Flag `library: true` modules separately (*library, nothing needs it*).
- **Unused systems:** installed, used by no world of that env.
- **Foundry version:** the newest `coreVersion` of the env's worlds, since the Data folder doesn't record the
  server's version. An env with no opened world has none, and its packages skip the Foundry check.
- **Compatibility:** each system and module's `compatibility` against the Foundry version, and each module's
  `relationships.systems[].compatibility` against the installed version of that system. Systems that aren't
  installed in the env are skipped. A package is *too new* (below `minimum`), *deprecated* (past `maximum`), or
  *unverified* (`verified` is an older major version; being builds behind doesn't count). A bare `maximum` such as
  `13` covers all of 13.x.
- **Drift:** for every system/module, its version per env compared to the reference env (first in
  `FOUNDRY_ENVS`): newer, older, missing, only here.

Compare versions with a tolerant parser (`packaging.version` with a string fallback); never crash on odd
version strings.

### 5.5 Inventory model

Written to `/state/inventory/current.json` after each collect; the previous one is kept as `previous.json`.
Suggested shape (pydantic models):

```json
{
  "generated_at": "2026-10-31T01:00:04+01:00",
  "envs": {
    "main": {
      "available": true,
      "systems": { "pf2e": { "title": "...", "version": "6.5.1", "...": "..." } },
      "modules": { "dice-so-nice": { "title": "...", "version": "5.1.2", "url": "...", "library": false, "requires": ["lib-wrapper"], "systems": [], "...": "..." } },
      "worlds":  { "strahd": { "title": "...", "system": "dnd5e", "system_version": "...", "core_version": "13.351",
                               "last_played": "...", "enabled": ["dice-so-nice", "..."], "stale": false } },
      "broken":  [ { "kind": "module", "folder": "foo", "error": "..." } ]
    }
  }
}
```

---

## 6. Changes

Diff `previous.json` and `current.json`. Events:

`system_installed`, `system_removed`, `system_updated (from → to)`, `module_installed`, `module_removed`,
`module_updated (from → to)`, `world_created`, `world_removed`, `module_enabled (world)`,
`module_disabled (world)`, `world_system_updated (from → to)`.

Each event carries the env. Append each night's events to `/state/changes/<YYYY-MM-DD>.json` (no file when
nothing changed). The first run ever produces no events (it's the baseline). Keep 365 days.

---

## 7. AI summaries

### 7.1 What gets summarised

One summary per **system or module id @ version**, shared by all envs. Two envs on different versions mean
two summaries; the page shows the reference env's version, or the newest if the reference env doesn't have it.

Cache key: `<kind>/<id>/<version>`. A cache entry is valid when its `prompt_version` equals
`WIKI_PROMPT_VERSION`. Bumping `WIKI_PROMPT_VERSION` regenerates everything over the following nights;
changing the model alone does not.

Queue order (most useful first):
1. enabled in a world of the reference env
2. enabled in a world of any env
3. systems
4. everything else
Within a group: newest-installed first (by the module folder's mtime).

### 7.2 Source text

Try in order, take the first that yields at least ~300 characters of text:

1. Manifest `readme` field, if it's an http(s) URL.
2. The repository README, from the repo URL found in `url`, else `bugs`, else `manifest`/`download`:
   - GitHub `github.com/<owner>/<repo>[/...]` → `GET https://api.github.com/repos/<owner>/<repo>/readme`
     with `Accept: application/vnd.github.raw` (token from `GITHUB_TOKEN` if set).
   - GitLab `gitlab.com/<path>` → `GET https://gitlab.com/api/v4/projects/<url-encoded path>/repository/files/README.md/raw?ref=HEAD`
     (try `README.md`, `readme.md`, `README`).
3. A README shipped in the package folder (`README.md`, `readme.md`, case-insensitive).
4. The manifest `description`. Mark the summary `source: "description"`, so the page can say it's brief.

Record which source was used and its URL (shown on the page).

Clean the text before sending: drop images, badges, HTML tags, link URLs (keep link text), install
instructions sections if easily detected, collapse whitespace. Truncate to `WIKI_SOURCE_MAX_CHARS`
(default 24000).

Fetching: timeout 20 s, follow redirects, 2 retries with backoff. Respect GitHub's rate limit: on 403/429
with `X-RateLimit-Remaining: 0`, stop fetching from GitHub for this run (remaining items fall through to the
local README or stay pending). Cache fetched source text by `<kind>/<id>/<version>` too, so a retried
summary doesn't fetch again.

**The source text is untrusted** (anyone can write a README). The model only produces text for a summary;
it never decides facts, links or HTML. See §8.4.

### 7.3 The conductor task

Submit with `LPUSH vram_conductor_queue <json>` on `vram-redis` (password `VRAM_REDIS_PASSWORD`):

```json
{
  "task_id": "<uuid4>",
  "target": "ollama",
  "endpoint": "/api/chat",
  "priority": "normal",
  "timeout": 600,
  "payload": {
    "model": "qwen3.5:9b",
    "think": false,
    "format": { "...JSON schema from 7.4...": "" },
    "options": { "num_ctx": 16384, "temperature": 0.2 },
    "messages": [
      { "role": "system", "content": "<system prompt>" },
      { "role": "user",   "content": "<facts + cleaned source text>" }
    ]
  }
}
```

- `priority`, `model`, `num_ctx`, `timeout` come from config.
- Don't send `stream` or `keep_alive`: the conductor forces `stream: false` and owns `keep_alive`.
- **Result:** poll `result:{task_id}` with `GETDEL` every ~2 s. The envelope's `status` must be `ok`;
  the answer is `body.message.content`, a JSON string. Validate it against the schema.
- **Is the GPU idle?** Read the Redis key `vram_conductor_queue:status` (JSON, refreshed by the conductor's
  heartbeat). Idle = its `running` and `waiting` lists are both empty. Poll every 30 s; any job, of any
  priority and from any client, restarts the quiet timer. If the key is missing or not refreshed for
  > 2 min, treat the conductor as unreachable. Verify the field names against VRAMConductor 1.2.1
  `scheduler.py` (`describe()`, `running`/`waiting`; job ids are in `id`).
- **No cancelling needed.** Nothing is submitted until the GPU has been quiet, so a skipped night leaves
  nothing in the queue. At the deadline at most `WIKI_LLM_MAX_INFLIGHT` short tasks remain; they run, and
  their results are collected next night (conductor `RESULT_TTL` is 24 h in our stack).
- **Don't flood the queue:** never more than `WIKI_LLM_MAX_INFLIGHT` (default 2) of our tasks queued.
- **Priority `normal`.** The run starts in silent hours, after the idle gate, so it needs no special
  treatment. Chats through the Ollama gateway are `high` and still go first. Transcriptions (Verbatim) are
  `background`: any already queued or running hold back our start (the idle gate counts every job), and
  any submitted after we started wait until our run ends (done or deadline), since background jobs only
  start after 10 min without normal work.

### 7.4 Output schema

```json
{
  "type": "object",
  "required": ["category", "summary", "features", "gm_tips", "player_facing", "confidence"],
  "properties": {
    "category": { "enum": ["Combat", "Character & Sheets", "Dice", "Maps & Scenes", "Tokens & Vision",
                           "Lighting & Effects", "Audio", "Journals & Handouts", "Automation",
                           "Content & Compendiums", "UI & Quality of Life", "GM Tools",
                           "Integration", "Library", "Game System", "Other"] },
    "summary":       { "type": "string", "description": "2–4 sentences: what it does, for a GM" },
    "features":      { "type": "array", "items": { "type": "string" }, "maxItems": 8 },
    "gm_tips":       { "type": "array", "items": { "type": "string" }, "maxItems": 5,
                       "description": "how to use it during a session: where to click, what to enable" },
    "player_facing": { "type": "boolean", "description": "do players see or use it directly" },
    "confidence":    { "enum": ["high", "medium", "low"],
                       "description": "low when the source text says little about what it does" }
  }
}
```

The category list is part of the prompt version.

### 7.5 Prompt

System prompt, roughly:

> You write short entries for a wiki read by tabletop game masters who use Foundry VTT. You are given
> facts about one Foundry package and text from its README. Describe what the package does and how a GM
> would use it during play. Use only the given text. If it doesn't say something, leave it out; never
> invent features, settings or commands. Plain English, no marketing tone, no Markdown, no HTML, no links.
> Answer with JSON matching the schema.

User message: kind, title, id, version, authors, game systems it is for, requires, compendium pack labels,
manifest description (HTML stripped), then the cleaned source text.

Systems get the same schema with `category: "Game System"`; the prompt asks for what the system offers a GM
(sheets, automation, compendium content).

### 7.6 Failures

| Case | Handling |
|---|---|
| `status: error`, timeout, invalid JSON, schema mismatch | Retry once in the same run. Then record a failure (`attempts`, `last_error`) in the cache |
| Failed 3 nights in a row for the same id@version | Stop trying until the version changes; page falls back to the manifest description |
| Redis or conductor unreachable | Log, treat as "AI skipped tonight", still publish facts |
| Model not installed (Ollama 404) | Same as above, with a clear error in the run report |

### 7.7 Timing rules (config defaults)

| Variable | Default | Meaning |
|---|---|---|
| `WIKI_LLM_IDLE_BEFORE_START` | `10m` | How long the conductor must have had no running or waiting jobs before the first task is submitted |
| `WIKI_LLM_START_WINDOW` | `2h` | Counted from run start. No quiet period within it → the AI part is skipped tonight |
| `WIKI_LLM_DEADLINE` | `07:00` | Local time after which no new tasks are submitted. In-flight ids go to `pending.json` |
| `WIKI_LLM_MAX_INFLIGHT` | `2` | Our tasks queued at the conductor at once |
| `WIKI_LLM_TASK_TIMEOUT` | `600` | `timeout` sent with each task (run time, not queue time) |
| `WIKI_LLM_RESULT_WAIT` | `3h` | Give up waiting for one task's result after this; its id goes to `pending.json` |

`pending.json` holds `{task_id, kind, id, version, submitted_at}`. Step 4 of the next run `GETDEL`s each;
results are used, missing ones older than 24 h are dropped (their item is simply queued again).

---

## 8. The site

### 8.1 Structure

```
Home                         what changed in the last 14 days; counts; last run status
Game Systems/
  index                      table: system, version per env, worlds using it
  <system-id>                summary, versions per env, worlds using it, modules made for it
Modules/
  index                      grouped by category: title, one-line summary, env tags
  <module-id>                see 8.2
Worlds/
  <env>/index                worlds in that env: title, system, last played, module count
  <env>/<world-id>           see 8.3
Housekeeping/
  unused                     per env: removal candidates, unused libraries, unused systems, broken folders
  compatibility              per env: deprecated, too new and unverified packages, with their world counts
  drift                      per system/module: versions per env vs. the reference env
Changes                      full change history, newest first
```

Top navigation tabs: Game Systems, Modules, Worlds, Housekeeping, Changes. Search enabled. Light/dark toggle.

### 8.2 Module page

```
# Dice So Nice!                                         [Dice]  [player-facing]

| Env    | Version | Enabled in                     |
|--------|---------|--------------------------------|
| Main   | 5.1.2   | Curse of Strahd, One-shots     |
| Stage  | 5.2.0 ⬆  | Curse of Strahd (copy)         |
| Canary | —       |                                |

<summary>

## Features
- ...

## At the table
- ...

Repository · Report a bug · Authors: ... · License: ...
Requires: libWrapper · Required by: — · Game systems: any
Compatible with Foundry: 12 – 13 (verified 13)

AI-generated from the README (link to source). Confidence: medium.
```

- ⬆/⬇ against the reference env; `—` = not installed there. Env names link to the world pages.
- No summary yet: show the manifest description and *Summary pending*.
- Low confidence or `source: description`: say the summary is brief and point to the repository.

### 8.3 World page

- Title, env, game system (linked) and version, Foundry core version, last played.
- World description only if `WIKI_SHOW_WORLD_DESCRIPTIONS=true` (default **false**: the site is public).
- Enabled modules grouped by category, each with its one-line summary and a link.
- Warnings: enabled but not installed; data *stale* (settings couldn't be read tonight).

### 8.4 Rendering rules

- Pages are Markdown generated from Jinja templates, then `mkdocs build` (not `--strict`).
- **All text from manifests, READMEs and the model is escaped** (HTML and Markdown special characters) before
  it goes into a page. The site is public; nothing from those sources may inject HTML or script.
- Links only from manifest fields, and only `http`/`https` URLs. The model never produces links.
- Page slugs are package/world ids (already URL-safe in Foundry); sanitise anyway.
- Footer: "Generated <time>. Facts from the Foundry data; summaries are AI-generated." and the last run status.

### 8.5 Publishing

1. Build into `/state/site/build-<timestamp>/`.
2. Atomically replace the symlink `/state/site/current` (create the new link, then `rename` over the old).
3. Delete old builds, keeping the newest 3.

nginx serves `/state/site/current` read-only and resolves the symlink per request, so a rebuild never
serves a half-written site.

---

## 9. State directory

```
/state/
  run.lock
  inventory/current.json, previous.json
  changes/<YYYY-MM-DD>.json
  cache/summaries/<kind>/<id>/<version>.json    {summary fields, prompt_version, model, source, source_url, created_at}
  cache/sources/<kind>/<id>/<version>.txt
  cache/failures.json
  pending.json
  reports/<YYYY-MM-DD>.json                     run report (see §11)
  site/current -> build-...
```

Write every JSON file atomically (temp file + rename).

---

## 10. Configuration

| Variable | Default | |
|---|---|---|
| `FOUNDRY_ENVS` | — | **Required.** YAML list of `name` and `path` (§5.1), or one line `name=path,...` |
| `WIKI_STATE_DIR` | `/state` | |
| `WIKI_SITE_URL` | — | e.g. `https://wiki.vtt.example.com`, for mkdocs `site_url` |
| `WIKI_SITE_NAME` | `Foundry VTT Wiki` | |
| `WIKI_SCHEDULE` | `0 1 * * *` | Cron expression for supercronic |
| `WIKI_RUN_ON_START` | `false` | Run once when the container starts |
| `WIKI_PUBLISH_WITHOUT_AI` | `true` | `false` = a night where AI is skipped publishes nothing |
| `WIKI_SHOW_WORLD_DESCRIPTIONS` | `false` | |
| `WIKI_LLM_ENABLED` | `true` | `false` = facts only |
| `WIKI_LLM_MODEL` | `qwen3.5:9b` | |
| `WIKI_LLM_NUM_CTX` | `16384` | |
| `WIKI_LLM_PRIORITY` | `normal` | `high`, `normal` or `background` |
| `WIKI_PROMPT_VERSION` | `1` | Bump to regenerate all summaries |
| `WIKI_SOURCE_MAX_CHARS` | `24000` | |
| `WIKI_LLM_START_WINDOW`, `WIKI_LLM_DEADLINE`, `WIKI_LLM_MAX_INFLIGHT`, `WIKI_LLM_TASK_TIMEOUT`, `WIKI_LLM_RESULT_WAIT` | see §7.7 | |
| `WIKI_REBUILD_EVERY` | `10` | Rebuild + publish after this many new summaries |
| `REDIS_HOST` / `REDIS_PORT` | `vram-redis` / `6379` | |
| `VRAM_REDIS_PASSWORD` | — | **Required** when the LLM is enabled |
| `CONDUCTOR_QUEUE` | `vram_conductor_queue` | Status is read from `<queue>:status` |
| `GITHUB_TOKEN` | — | Optional, read-only public access. Strongly recommended for the first run |
| `TZ` | `Europe/Stockholm` | Schedule and deadline are local time |
| `LOG_LEVEL` | `INFO` | |

Validate config at startup and fail with a clear message.

---

## 11. Operations

### CLI

```
wikigen run                 the full nightly run (§4)
wikigen collect [--json]    collect and print the inventory, write nothing
wikigen build               collect + diff + build + publish, no AI
wikigen enrich [--limit N] [--ignore-window]   AI step only
wikigen status              last run report, cache coverage, pending tasks
wikigen forget <kind> <id> [<version>]         drop a cached summary so it is regenerated
```

The container's default command starts supercronic with a crontab generated from `WIKI_SCHEDULE`.

### Logging and run report

Structured logs to stdout (one line per event). Each run writes `reports/<date>.json` with start/end, envs
collected (or unavailable), change count, summaries done / pending / failed, and the AI outcome: `completed`,
`deadline reached`, `skipped: GPU busy`, `skipped: conductor unreachable`, `disabled`. The site footer shows only
when the site was last updated.

### Healthcheck

The container is healthy if supercronic is running. A failed run must not crash the container.

### Container

- Runs as **uid/gid 1000:1000** by default: the Foundry data on NFS is owned by 1000:1000, and `/state` will
  be too. Don't hard-code it; the stack can override it with `user:`.
- `/foundry` read-only, `/state` read-write.
- Ship `build.ps1` / `push.ps1` like VRAMConductor's (tags, `-Registry`, `-Push`). Immutable version tags.

### Local development

`compose.yaml` that mounts a local copy of a `Data` dir (or the NFS share) and points `REDIS_HOST` at the GPU
node's LAN address (VRAMConductor exposes `6379` on the LAN), serving the site on `localhost:8000`.

---

## 12. Tests and acceptance criteria

Unit tests with fixtures under `tests/fixtures/` (a fake `Data/` tree with systems, modules, worlds, and a
real LevelDB `settings` dir created by the test setup with `plyvel`, including a `LOCK` file held open):

1. Collect reads systems, modules and worlds from two envs; malformed manifests are reported as broken, not fatal.
2. Enabled modules are read from a **locked** LevelDB settings dir via the copy, and from a NeDB `settings.db`.
3. A world whose settings can't be read falls back to the previous inventory and is marked stale.
4. Removal candidates exclude modules required (transitively) by enabled modules; libraries are flagged.
5. Drift: newer, older, missing and only-here are detected against the reference env; odd version strings don't crash.
6. Diff produces the right events; the first run produces none.
7. Source resolution order (manifest `readme` → GitHub/GitLab → local README → description) with mocked HTTP; GitHub rate limiting stops further GitHub calls.
8. Conductor client against a fake Redis (`fakeredis`): task shape, `GETDEL` result handling, idle detection from the status key, stale/missing status key = unreachable, max in-flight.
9. Idle gate: nothing is submitted until the status has been empty for the full quiet period; a job appearing restarts the timer; no quiet period within the window → nothing submitted, run reports `skipped: GPU busy`, facts still published. With `WIKI_PUBLISH_WITHOUT_AI=false` nothing is published.
10. Deadline: no submissions after it; in-flight ids land in `pending.json` and are collected by the next run.
11. Invalid model output is retried once, then recorded as a failure; 3 nights of failure stop retries until the version changes.
12. Cache: a new version or bumped `WIKI_PROMPT_VERSION` regenerates; a model change alone does not.
13. Rendering escapes HTML/script in manifests, READMEs and model output; only http(s) links are rendered.
14. Publishing swaps the `current` symlink atomically and keeps 3 builds.
15. A second `wikigen run` while one is running exits without doing anything.

Manual acceptance on the swarm:

- First run with an empty cache: the site is up with all facts within minutes; summaries appear over one or
  more nights.
- Install a module on stage → next morning it's on the wiki with its stage version and *Summary pending* or a
  summary; drift shows it as stage-only.
- Enable it in a world → next morning the world page lists it and the change log shows `module_enabled`.
- Start a long GPU job before 01:00 that runs past 03:00 → the run report says `skipped: GPU busy`, facts
  are updated, and the conductor dashboard never showed a task of ours.
- Start a GPU job that ends at 01:30 → summaries start at about 01:40.

---

## 13. Open points

- **Foundry core version per env:** v1 shows each world's `coreVersion`. If an env-level version is wanted,
  add an optional `name=path@version` in `FOUNDRY_ENVS` or read it from the felddy container cache.
- **Changelog summaries** ("what's new in 5.2 for GMs") are a natural v2, using the manifest `changelog` URL.
