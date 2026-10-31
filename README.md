# Foundry VTT Wiki Generator

A nightly job that inventories every Foundry VTT environment and publishes a static wiki for GMs. The facts
come from Foundry's own files; an LLM only turns READMEs into short summaries. The full design is in
[Implementation Specification.md](Implementation%20Specification.md).

## Run it locally

`plyvel` has no Windows wheels, so everything runs in Docker.

```powershell
Copy-Item .env.example .env        # then set the Data folders
docker compose up -d --build
docker compose exec generator wikigen run
```

The site is on <http://localhost:8000>, or the port in `WIKI_LOCAL_PORT`. The Foundry data is mounted read-only.
State lives in the `state` volume, not a bind mount, because publishing needs real symlinks.

For AI summaries, set `WIKI_LLM_ENABLED=true`, `REDIS_HOST` and `VRAM_REDIS_PASSWORD` in `.env`, recreate the
container with `docker compose up -d`, then:

```powershell
docker compose exec generator wikigen enrich --limit 3 --ignore-window   # a few, at once
docker compose exec generator wikigen status
```

`--ignore-window` skips the idle gate, so tasks go to the conductor even while the GPU is busy.

To test by hand, set `WIKI_SCHEDULE=off` for no nightly run and `WIKI_LLM_IDLE_BEFORE_START=0` so `wikigen run`
starts AI as soon as the conductor reports idle.

## Environments

The app reads one setting, `FOUNDRY_ENVS`: a YAML list of environments. It is set in the service's `environment:`
block, so locally it is in `compose.yaml` and on the swarm in `stack.yaml`, not in `.env`. The folders need no
shared root, because each one gets its own mount.

An environment takes three pieces: a list entry, a mount, and the real folder.

```yaml
services:
  generator:
    environment:
      FOUNDRY_ENVS: |
        - name: main
          path: /foundry/main
        - name: stage
          path: /foundry/stage
        - name: canary
          path: /foundry/canary
    volumes:
      - ${FOUNDRY_MAIN_DATA}:/foundry/main:ro
      - ${FOUNDRY_STAGING_DATA}:/foundry/stage:ro
      - ${FOUNDRY_CANARY_DATA}:/foundry/canary:ro
      - ${DATA_PATH}/web/foundry-wiki:/state
```

```bash
# .env
FOUNDRY_MAIN_DATA=/path/to/main/data/Data
FOUNDRY_STAGING_DATA=/path/to/staging/data/Data
FOUNDRY_CANARY_DATA=/path/to/canary/data/Data
```

| Piece | Rule |
|---|---|
| `name` | Lower-case letters, digits, `-` or `_`. Sets the label and badge: `main` is a green **Main**, `stage` an orange **Stage**, `canary` a purple **Canary**. Any other name works but gets a grey badge until it has a colour in `wiki.css`, so `staging` would show as a grey **Staging** |
| `path` | Where the folder is inside the container. Any path, as long as it matches the right-hand side of the mount |
| Mount, left side | The Foundry `Data` folder, the one containing `systems/`, `modules/` and `worlds/`. Always mount it `:ro` |
| `FOUNDRY_*_DATA` | Only compose variables, never read by the app. The names are free, and a stack can write the NFS path directly in the mount instead |

- List order is display order, and the first env is the drift reference.
- Add an env's entry and mount together, once its folder exists: Swarm refuses to start a task whose bind-mount
  source is missing.
- There is no limit on the number of envs.
- A single line also works: `FOUNDRY_ENVS=main=/foundry/main,stage=/foundry/stage`.

## Before the swarm stack

- Don't carry over the local test settings: leave `WIKI_SCHEDULE` and `WIKI_LLM_IDLE_BEFORE_START` unset so the
  nightly run and the 10-minute idle gate apply.
- Set `TZ`, `WIKI_SITE_URL`, `REDIS_HOST`, `VRAM_REDIS_PASSWORD` and `GITHUB_TOKEN`. Keep the password and token out
  of the repo.
- `/state` holds publish symlinks, so it must be a real Linux filesystem such as the NFS share. nginx mounts the
  same `/state` read-only with `nginx/default.conf`.
- Check the conductor's `RESULT_TTL` (24 h) and `REQUEST_TIMEOUT` (at least `WIKI_LLM_TASK_TIMEOUT`).
- Check world descriptions for spoilers: they are public by default.
- A run killed part-way leaves `run.lock`, and later runs exit for up to 12 hours. If no run is active, clear it
  with `rm /state/run.lock`.

## Commands

| Command | |
|---|---|
| `wikigen run` | The full nightly run |
| `wikigen collect [--json]` | Collect and print the inventory, write nothing |
| `wikigen build` | Collect, diff, build and publish, no AI |
| `wikigen enrich [--limit N] [--ignore-window]` | AI step only |
| `wikigen status` | Last run report, cache coverage, pending tasks |
| `wikigen forget <kind> <id> [<version>]` | Drop a cached summary so it is regenerated |

## Tests

```powershell
docker compose --profile test run --rm --build test
```

## Build and push

The registry comes from `DOCKER_REGISTRY`, with or without `https://`, or from `-Registry`. Tags default to the
version in `pyproject.toml` and `latest`.

```powershell
.\build.ps1                    # build and tag
.\build.ps1 -Push              # build, tag and push
.\build.ps1 -Tag 0.2.0-rc1     # build a specific tag
.\push.ps1                     # push what's already built
```

Version tags are immutable: pushing one that's already in the registry is refused. Bump the version in
`pyproject.toml`, or pass `-Force` to `push.ps1`. Run `docker login` for the registry first.

## Notes

- Windows junctions in a `Data` folder dangle inside Linux containers. They show up as broken folders on the
  Unused page, and worlds enabling them list them by id under *Other*.
- World descriptions are shown by default and the site is public. Set `WIKI_SHOW_WORLD_DESCRIPTIONS=false` to hide
  them.
- The Compatibility page takes each env's Foundry version from its worlds, because the Data folder doesn't record
  it. After an upgrade, the new version only shows once a world has been opened on it.
- The idle gate also waits for conductor holds (`holders` in the status key), since tasks can't run during one.
- Keep `RESULT_TTL` on the conductor at 24 h. Its default is 1 h, which loses results parked at the deadline;
  those items are simply queued again the next night.
- `WIKI_LLM_DEADLINE` is the next occurrence after the run starts, so a run started by hand in the day isn't
  already past it.
