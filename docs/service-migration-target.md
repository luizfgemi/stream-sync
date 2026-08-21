# Stream Sync Service Migration Target

## Goal

Move Stream Sync from a daemon script shape toward an internal service-oriented
application shape. The daemon and the HTTP API should call the same Python
services directly. HTTP endpoints remain an external interface, not the internal
path used by the daemon.

## Current Shape

- `main.py` owns bootstrap, signal handling, scheduling, dependency construction,
  cycle execution, deletion handling, runtime status writes, and CLI modes.
- `api.py` owns HTTP routing, auth, config responses, snapshot reads, and direct
  Radarr writes for favorite changes.
- Client/provider modules already have reasonable boundaries:
  - `radarr_client.py` is a Radarr adapter.
  - `justwatch_provider.py` is a JustWatch provider.
  - `seerr_client.py` is a Seerr adapter.
  - `notifier.py` is notification infrastructure.
  - `cache_sqlite.py` is persistence.
  - `policy.py`, `recent_release.py`, `snapshot.py`, and `types.py` are close to
    domain/helper modules.

## Target Shape

```text
app/
  main.py                 # entrypoint and minimal bootstrap
  api.py                  # HTTP routes only
  daemon.py               # long-running scheduler/runner
  services/
    config_service.py     # effective config and API overrides
    movie_service.py      # movie reads/search and direct movie actions
    deletion_service.py   # deletion queue, path sizing, folder removal
    sync_cycle_service.py # full scan orchestration
    daemon_service.py     # reload config, rebuild clients, schedule cycles
```

## Services

### ConfigService

Owns the application-level config API:

- Redacted effective config payload.
- Update persisted API overrides.
- Reset persisted API overrides.
- Runtime event emission for config updates/resets.

This moves the config closures currently inside `api.py` into reusable code.

### MovieService

Owns movie read actions and small direct mutations:

- List/search movie snapshots from SQLite.
- Fetch a single movie snapshot.
- Set/remove favorite tag in Radarr.
- Refresh the affected movie snapshot after favorite changes.

This removes direct `RadarrClient` usage from endpoint handlers.

### DeletionService

Owns filesystem-sensitive deletion behavior:

- Safe movie folder deletion.
- Directory sizing.
- Deletion queue summary.
- Later: deletion due-pass execution and suppression logic.

This isolates the highest-risk side effects from cycle orchestration.

### SyncCycleService

Owns one full scan cycle:

- Set runtime status and events.
- Load Radarr movie state.
- Load Seerr protection.
- Query JustWatch.
- Evaluate policy.
- Coordinate Radarr updates/searches.
- Create/update snapshots.
- Coordinate deletion scheduling and deletion execution through
  `DeletionService`.

Initial migration should move `_run_cycle` behavior here without changing
behavior. Later passes can split smaller helpers out.

### DaemonService

Owns long-running execution:

- Load effective config before each cycle.
- Rebuild JustWatch/Seerr/notifier dependencies when config changes.
- Validate `JW_ALLOWED_SERVICES`.
- Call `SyncCycleService.run_cycle`.
- Sleep until the next cycle with stop-event awareness.

This leaves `main.py` as wiring only.

## Migration Order

1. Add application package and migration target document.
2. Move config and movie API behavior into `ConfigService` and `MovieService`.
3. Change `api.py` to call the services.
4. Move daemon loop into `DaemonService`.
5. Move `_run_cycle` into `SyncCycleService` with no behavior changes.
6. Extract deletion-specific code into `DeletionService`.
7. Add focused unit tests for each service.

## Boundary Rules

- API handlers should validate HTTP/auth details and call services.
- Daemon code should call services directly, not HTTP endpoints.
- External systems stay behind adapters/providers.
- Pure policy code should stay pure and easily unit-testable.
- Filesystem deletion must remain isolated and testable.
