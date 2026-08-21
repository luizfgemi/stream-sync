from __future__ import annotations

import logging
import random
import re
import threading
import time
from typing import Any

import requests
from simplejustwatchapi.justwatch import offers_for_countries, search

from .cache_sqlite import SQLiteCache
from .types import JwLookupResult, JwService, LookupStatus, MovieState


class JustWatchProvider:
    def __init__(
        self,
        cache: SQLiteCache | None,
        country: str,
        language: str,
        only_subscription: bool = True,
        request_delay_seconds: float = 2.0,
        request_delay_jitter_seconds: float = 1.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._logger = logging.getLogger("app.justwatch")
        self._cache = cache
        self._country = country.upper()
        self._language = language
        self._only_subscription = only_subscription
        self._graphql_url = "https://apis.justwatch.com/graphql"
        self._request_delay_seconds = max(0.0, float(request_delay_seconds))
        self._request_delay_jitter_seconds = max(
            0.0, float(request_delay_jitter_seconds)
        )
        self._stop_event = stop_event
        self._request_lock = threading.Lock()
        self._next_request_not_before = 0.0

    @staticmethod
    def _unique_non_empty(values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = str(value or "").strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(normalized)
        return out

    def _wait_before_request(self) -> None:
        if self._request_delay_seconds <= 0 and self._request_delay_jitter_seconds <= 0:
            return

        with self._request_lock:
            now = time.monotonic()
            if now < self._next_request_not_before:
                remaining = self._next_request_not_before - now
                if self._stop_event is not None:
                    if self._stop_event.wait(remaining):
                        raise InterruptedError("Stop requested before JustWatch request")
                else:
                    time.sleep(remaining)

            jitter = (
                random.uniform(
                    -self._request_delay_jitter_seconds,
                    self._request_delay_jitter_seconds,
                )
                if self._request_delay_jitter_seconds > 0
                else 0.0
            )
            next_delay = max(0.0, self._request_delay_seconds + jitter)
            self._next_request_not_before = time.monotonic() + next_delay

    def lookup_movie(
        self, movie: MovieState, enabled_services: set[str] | None = None
    ) -> JwLookupResult:
        if self._cache is None:
            raise RuntimeError("lookup_movie requires an initialized SQLite cache")

        if movie.tmdb_id is None:
            return JwLookupResult(
                status=LookupStatus.UNKNOWN,
                error_message="Movie has no TMDB ID in Radarr",
            )

        cached_node_id = self._cache.get_jw_node_id(movie.tmdb_id)
        node_id = cached_node_id
        if not node_id:
            node_id = self._resolve_node_id(movie)
            if not node_id:
                return JwLookupResult(
                    status=LookupStatus.UNKNOWN,
                    error_message="Could not resolve jw_node_id from TMDB",
                )
            self._cache.set_jw_node_id(movie.tmdb_id, node_id)

        try:
            cache_entry = self._cache.get_offers(node_id, self._country)
        except ValueError as exc:
            return JwLookupResult(
                status=LookupStatus.SCHEMA_ERROR,
                error_message=f"Cache schema error: {exc}",
            )

        if cache_entry is not None:
            if cache_entry.is_error:
                return JwLookupResult(
                    status=LookupStatus.UNKNOWN,
                    error_message=cache_entry.error_message or "Cached JustWatch error",
                )
            try:
                services = self._extract_services(cache_entry.payload, enabled_services)
            except ValueError as exc:
                return JwLookupResult(
                    status=LookupStatus.SCHEMA_ERROR,
                    error_message=f"Cached payload schema error: {exc}",
                )
            return JwLookupResult(
                status=LookupStatus.AVAILABLE if services else LookupStatus.UNAVAILABLE,
                services=services,
            )

        payload, fetch_error = self._fetch_payload_for_node(node_id)
        if fetch_error is not None and cached_node_id:
            return self._retry_after_idmap_refresh(
                movie=movie,
                enabled_services=enabled_services,
                previous_node_id=node_id,
                previous_error=f"JustWatch request failed: {fetch_error}",
            )
        if fetch_error is not None:
            self._cache.set_offers_error(node_id, self._country, str(fetch_error))
            return JwLookupResult(
                status=LookupStatus.UNKNOWN,
                error_message=f"JustWatch request failed: {fetch_error}",
            )

        assert payload is not None
        try:
            services = self._extract_services(payload, enabled_services)
        except ValueError as exc:
            if cached_node_id:
                return self._retry_after_idmap_refresh(
                    movie=movie,
                    enabled_services=enabled_services,
                    previous_node_id=node_id,
                    previous_error=f"JustWatch parsing/schema error: {exc}",
                )
            return JwLookupResult(
                status=LookupStatus.SCHEMA_ERROR,
                error_message=f"JustWatch parsing/schema error: {exc}",
            )

        self._cache.set_offers_ok(node_id, self._country, payload)
        return JwLookupResult(
            status=LookupStatus.AVAILABLE if services else LookupStatus.UNAVAILABLE,
            services=services,
        )

    def _fetch_payload_for_node(self, node_id: str) -> tuple[dict[str, Any] | None, Exception | None]:
        try:
            self._wait_before_request()
            raw = offers_for_countries(
                node_id,
                {self._country},
                self._language,
                True,
            )
            payload = self._build_payload(raw)
            return payload, None
        except InterruptedError:
            raise
        except Exception as exc:
            return None, exc

    def _retry_after_idmap_refresh(
        self,
        movie: MovieState,
        enabled_services: set[str] | None,
        previous_node_id: str,
        previous_error: str,
    ) -> JwLookupResult:
        if self._cache is None or movie.tmdb_id is None:
            return JwLookupResult(
                status=LookupStatus.UNKNOWN,
                error_message=f"{previous_error} (after idmap refresh unavailable)",
            )

        self._logger.warning(
            "Cached jw_node_id failed for '%s' (tmdb=%s node_id=%s). Refreshing id map.",
            movie.title,
            movie.tmdb_id,
            previous_node_id,
        )
        self._cache.delete_jw_node_id(movie.tmdb_id)

        refreshed_node_id = self._resolve_node_id(movie)
        if not refreshed_node_id:
            return JwLookupResult(
                status=LookupStatus.UNKNOWN,
                error_message=f"{previous_error} (after idmap refresh: could not resolve jw_node_id)",
            )

        self._cache.set_jw_node_id(movie.tmdb_id, refreshed_node_id)
        payload, fetch_error = self._fetch_payload_for_node(refreshed_node_id)
        if fetch_error is not None:
            self._cache.set_offers_error(refreshed_node_id, self._country, str(fetch_error))
            return JwLookupResult(
                status=LookupStatus.UNKNOWN,
                error_message=f"JustWatch request failed after idmap refresh: {fetch_error}",
            )

        assert payload is not None
        try:
            services = self._extract_services(payload, enabled_services)
        except ValueError as exc:
            return JwLookupResult(
                status=LookupStatus.SCHEMA_ERROR,
                error_message=f"JustWatch parsing/schema error after idmap refresh: {exc}",
            )

        self._cache.set_offers_ok(refreshed_node_id, self._country, payload)
        return JwLookupResult(
            status=LookupStatus.AVAILABLE if services else LookupStatus.UNAVAILABLE,
            services=services,
        )

    def list_country_services(self, country: str | None = None) -> list[JwService]:
        selected_country = (country or self._country).upper()
        query = """
query GetPackages($country: Country!, $platform: Platform!) {
  packages(country: $country, platform: $platform) {
    clearName
    technicalName
  }
}
"""
        body = {
            "operationName": "GetPackages",
            "variables": {"country": selected_country, "platform": "WEB"},
            "query": query,
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": f"{self._language},en-US;q=0.8,en;q=0.7",
            "Content-Type": "application/json",
            "Origin": "https://www.justwatch.com",
            "Referer": "https://www.justwatch.com/",
        }

        response = requests.post(
            self._graphql_url,
            json=body,
            headers=headers,
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("GraphQL response missing data field")
        packages = data.get("packages")
        if not isinstance(packages, list):
            raise ValueError("GraphQL response missing packages list")

        services_by_id: dict[str, JwService] = {}
        for package in packages:
            if not isinstance(package, dict):
                continue
            technical_name = str(package.get("technicalName", "")).strip()
            label = str(package.get("clearName", "")).strip()
            base = technical_name or label
            if not base:
                continue

            service_id = self.normalize_service_id(base)
            if not service_id:
                continue

            service_label = label if label else self._service_name_from_id(service_id)
            services_by_id[service_id] = JwService(
                service_id=service_id,
                service_name=service_label,
            )

        return sorted(services_by_id.values(), key=lambda item: item.service_name.lower())

    def _resolve_node_id(self, movie: MovieState) -> str | None:
        tmdb_id = str(movie.tmdb_id)

        query_candidates = [movie.title]
        if movie.year and int(movie.year) > 0:
            query_candidates.append(f"{movie.title} {movie.year}")
        queries = self._unique_non_empty(query_candidates)

        countries = self._unique_non_empty([self._country, "US", "GB"])
        languages = self._unique_non_empty([self._language, "en-US", "en"])

        for country in countries:
            for language in languages:
                for query in queries:
                    try:
                        self._wait_before_request()
                        results = search(
                            query,
                            country,
                            language,
                            30,
                            False,
                        )
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        self._logger.warning(
                            "Failed to search title in JustWatch: %s (tmdb=%s country=%s lang=%s query=%s): %s",
                            movie.title,
                            movie.tmdb_id,
                            country,
                            language,
                            query,
                            exc,
                        )
                        continue

                    for entry in results:
                        entry_tmdb = getattr(entry, "tmdb_id", None)
                        entry_id = getattr(entry, "entry_id", None)
                        if entry_tmdb is None or entry_id is None:
                            continue
                        if str(entry_tmdb) == tmdb_id:
                            if country != self._country:
                                self._logger.info(
                                    "Resolved jw_node_id for '%s' using fallback country=%s (offers still queried in %s)",
                                    movie.title,
                                    country,
                                    self._country,
                                )
                            return str(entry_id)
        return None

    def _build_payload(self, offers_map: Any) -> dict[str, Any]:
        if not isinstance(offers_map, dict):
            raise ValueError("Offers response is not an object")

        offers = offers_map.get(self._country) or offers_map.get(self._country.upper())
        if offers is None:
            offers = []
        if not isinstance(offers, list):
            raise ValueError("Offers field is not a list")

        payload_offers: list[dict[str, Any]] = []
        for offer in offers:
            monetization_type = getattr(offer, "monetization_type", None)
            package = getattr(offer, "package", None)
            if monetization_type is None or package is None:
                raise ValueError("Offer missing monetization_type/package")

            technical_name = getattr(package, "technical_name", None)
            display_name = getattr(package, "name", None)
            payload_offers.append(
                {
                    "monetization_type": str(monetization_type),
                    "technical_name": str(technical_name) if technical_name else "",
                    "name": str(display_name) if display_name else "",
                }
            )

        return {"offers": payload_offers}

    def _extract_services(
        self, payload: dict[str, Any] | None, enabled_services: set[str] | None = None
    ) -> list[JwService]:
        if payload is None:
            raise ValueError("Missing offers payload")
        offers = payload.get("offers")
        if not isinstance(offers, list):
            raise ValueError("Payload missing offers list")

        allowed = {"FLATRATE", "SUBSCRIPTION"}
        enabled_normalized = (
            {item.strip().lower() for item in enabled_services}
            if enabled_services is not None
            else None
        )
        services_by_id: dict[str, JwService] = {}
        for offer in offers:
            if not isinstance(offer, dict):
                raise ValueError("Offer is not an object")

            monetization_type = str(offer.get("monetization_type", "")).upper()
            if self._only_subscription and monetization_type not in allowed:
                continue
            if not self._only_subscription and monetization_type == "":
                continue

            technical_name = str(offer.get("technical_name", "")).strip()
            display_name = str(offer.get("name", "")).strip()
            base = technical_name or display_name
            if not base:
                raise ValueError("Offer missing technical_name/name")

            service_id = self.normalize_service_id(base)
            if not service_id:
                raise ValueError("service_id is empty after normalization")
            if enabled_normalized is not None and service_id not in enabled_normalized:
                continue

            if service_id in services_by_id:
                continue

            service_name = display_name if display_name else self._service_name_from_id(service_id)
            services_by_id[service_id] = JwService(
                service_id=service_id,
                service_name=service_name,
            )

        return sorted(services_by_id.values(), key=lambda item: item.service_name.lower())

    @staticmethod
    def normalize_service_id(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.lower())
        return normalized.strip("_")

    @staticmethod
    def _service_name_from_id(service_id: str) -> str:
        known = {
            "amazonprimevideo": "Prime Video",
            "primevideo": "Prime Video",
            "primevideo_withads": "Prime Video (with ads)",
            "disneyplus": "Disney+",
            "appletvplus": "Apple TV+",
            "hbomax": "HBO Max",
            "max": "Max",
            "paramountplus": "Paramount+",
            "globoplay": "Globoplay",
        }
        if service_id in known:
            return known[service_id]
        return service_id.replace("_", " ").title()
