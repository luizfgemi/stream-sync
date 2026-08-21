# Stream Sync

Container-first Python microservice to reconcile Radarr movie libraries with JustWatch streaming availability, Seerr requests, and Plex Watchlists, providing configurable deletion grace periods and auto-tagging.

## Development

Requires Python 3.12.

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt pytest
./.venv/bin/python -m pytest
```

Run service locally:

```bash
./.venv/bin/python -m app.main
```

## Endpoints

- `GET /health` - Service healthcheck endpoint.
- `GET /api/v1/status` - Current synchronization status and last run timestamp.
- `POST /api/v1/sync` - Trigger an immediate manual sync cycle.
- `GET /api/v1/config` - Retrieve active service configuration settings.

## Key Features

- **JustWatch Integration**: Checks streaming availability for target countries and applies `streaming-*` tags or unmonitors movies.
- **Seerr & Plex Watchlist Protection**: Protects active requests or items on Plex watchlists from unmonitoring/deletion.
- **Graceful Deletion**: Configurable `REMOVE_MODE` (`report` or `delete`) with grace period countdowns (`DELETE_AFTER_DAYS`) before library purge.
- **Recent Release Protection**: Shields theatrical releases during `THEATRICAL_RELEASE_GRACE_MONTHS`.
- **Telegram Notifications**: Dispatches sync updates, pending deletion reminders, and library cleanup events.
