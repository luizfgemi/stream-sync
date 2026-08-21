from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config
from .plex_watchlist import PlexWatchlistClient
from .servarr_client import ServarrClient


@dataclass(frozen=True, slots=True)
class WatchlistSyncStats:
    users: int = 0
    movies_added: int = 0
    series_added: int = 0
    existing: int = 0
    skipped: int = 0
    errors: int = 0


def sync_plex_watchlists(config: Config) -> WatchlistSyncStats:
    logger = logging.getLogger("app.watchlist_sync")
    plex = PlexWatchlistClient(
        token=config.plex_token,
        token_file=config.plex_token_file,
        include_friends=config.plex_watchlist_include_friends,
    )
    radarr = ServarrClient(config.radarr_url, config.radarr_api_key, "radarr")
    sonarr = (
        ServarrClient(config.sonarr_url, config.sonarr_api_key, "sonarr")
        if config.sonarr_url and config.sonarr_api_key
        else None
    )
    items = plex.fetch_all()
    users = {user for item in items for user in item.users}
    movies_added = series_added = existing = skipped = errors = 0
    for item in items:
        try:
            if config.dry_run:
                skipped += 1
                logger.info(
                    "[DRY RUN] Would sync Plex watchlist item: %s type=%s users=%s",
                    item.title,
                    item.media_type,
                    ",".join(item.users),
                )
                continue
            if item.media_type == "movie":
                if item.tmdb_id is None:
                    skipped += 1
                    logger.warning(
                        "Skipping Plex watchlist movie without TMDB ID: %s",
                        item.title,
                    )
                    continue
                result = radarr.add_movie(
                    item.tmdb_id,
                    item.users,
                    config.plex_watchlist_radarr_profile_id,
                    config.plex_watchlist_radarr_root_folder,
                    config.plex_watchlist_search_on_add,
                )
                movies_added += int(result == "added")
                existing += int(result == "exists")
            else:
                if sonarr is None or item.tvdb_id is None:
                    skipped += 1
                    logger.warning(
                        "Skipping Plex watchlist series without Sonarr/TVDB ID: %s",
                        item.title,
                    )
                    continue
                result = sonarr.add_series(
                    item.tvdb_id,
                    item.users,
                    config.plex_watchlist_sonarr_profile_id,
                    config.plex_watchlist_sonarr_root_folder,
                    config.plex_watchlist_search_on_add,
                )
                series_added += int(result == "added")
                existing += int(result == "exists")
        except Exception as exc:
            errors += 1
            logger.error("Could not sync Plex watchlist item '%s': %s", item.title, exc)
    stats = WatchlistSyncStats(
        users=len(users),
        movies_added=movies_added,
        series_added=series_added,
        existing=existing,
        skipped=skipped,
        errors=errors,
    )
    logger.info(
        "Plex watchlist sync finished: users=%s movies_added=%s series_added=%s "
        "existing=%s skipped=%s errors=%s",
        stats.users,
        stats.movies_added,
        stats.series_added,
        stats.existing,
        stats.skipped,
        stats.errors,
    )
    return stats
