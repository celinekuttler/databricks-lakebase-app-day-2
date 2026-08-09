"""
Client for the National Weather Service (NWS) API - no API key required.

Mirrors massive_client.py's shape but for unstructured weather text. Given a
list of locations (city/state strings or "lat,lon" pairs), it:

1. Resolves each location to a lat/lon pair (Open-Meteo geocoding API for
   city/state strings; lat/lon pairs are used as-is).
2. Resolves the lat/lon to an NWS grid point + forecast office via
   GET /points/{lat},{lon}.
3. Fetches active weather alerts (GET /alerts/active?point=lat,lon), the
   latest Area Forecast Discussion (GET /products/types/AFD/locations/{office}),
   and the gridpoint forecast periods (GET .../forecast, whose detailedForecast
   text is a first-class narrative source).
4. Normalizes each alert/forecast item into a document record with a stable
   dedup id, so callers can upsert into Lakebase with ON CONFLICT (id).

The NWS API requires a descriptive User-Agent header and is free/rate-limited
(leniently). We always send one and keep one in-flight timeout for safety.
"""

import hashlib
import time
from datetime import datetime, timezone

import requests

_BASE_URL = "https://api.weather.gov"
_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_USER_AGENT = "databricks-lakebase-weather-app/1.0 (homework assignment)"

_DEFAULT_TIMEOUT = 30


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class WeatherClient:
    """Thin wrapper around the NWS API + Open-Meteo geocoding."""

    def __init__(self, base_url: str = _BASE_URL, timeout: int = _DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Accept": "application/geo+json",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Location resolution
    # ------------------------------------------------------------------
    def geocode(self, location: str) -> tuple[float, float] | None:
        """
        Resolve a "City, ST" / "City, Country" string to (lat, lon) using the
        Open-Meteo geocoding API (free, no key). Returns None if no match.
        """
        resp = self._session.get(
            _GEOCODE_URL,
            params={"name": location.strip(), "count": 1, "language": "en"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            return None
        return results[0]["latitude"], results[0]["longitude"]

    @staticmethod
    def _parse_coords(location: str) -> tuple[float, float] | None:
        """
        If the location string is already a "lat,lon" pair (e.g. "41.88,-87.63"),
        return it as-is. Otherwise return None so callers fall back to geocoding.
        """
        parts = [p.strip() for p in location.split(",")]
        if len(parts) != 2:
            return None
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return lat, lon

    def resolve(self, location: str) -> tuple[float, float]:
        """Return (lat, lon) for a location string, geocoding if necessary."""
        coords = self._parse_coords(location)
        if coords:
            return coords
        coords = self.geocode(location)
        if coords is None:
            raise ValueError(f"Could not resolve location to coordinates: {location!r}")
        return coords

    def get_points(self, lat: float, lon: float) -> dict:
        """GET /points/{lat},{lon} - gridpoint, forecast office, relative location."""
        resp = self._session.get(f"{self.base_url}/points/{lat:.4f},{lon:.4f}", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get("properties", {})

    # ------------------------------------------------------------------
    # NWS data fetchers
    # ------------------------------------------------------------------
    def get_active_alerts(self, lat: float, lon: float) -> list[dict]:
        """GET /alerts/active?point={lat},{lon} - returns raw alert properties."""
        resp = self._session.get(
            f"{self.base_url}/alerts/active",
            params={"point": f"{lat:.4f},{lon:.4f}"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        features = resp.json().get("features", []) or []
        return [f.get("properties", {}) for f in features]

    def get_product(self, product_id: str) -> dict | None:
        """
        GET /products/{product_id} - returns the full product, including the
        free-text `productText` body. NOTE: unlike most NWS endpoints, the
        product fields live at the TOP level of the response, not under
        "properties".
        """
        resp = self._session.get(
            f"{self.base_url}/products/{product_id}",
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_forecast_discussion(self, office: str) -> dict | None:
        """
        Returns the most recent Area Forecast Discussion WITH a non-empty
        `productText` body for the given NWS forecast office, or None if none
        exist. Iterates newest-first because the very latest product can still
        be an empty stub right after issuance.
        """
        resp = self._session.get(
            f"{self.base_url}/products/types/AFD/locations/{office}",
            timeout=self.timeout,
        )
        resp.raise_for_status()
        products = resp.json().get("@graph", []) or []
        products = sorted(products, key=lambda p: p.get("issuanceTime", ""), reverse=True)
        for product in products:
            product_id = product.get("id")
            if not product_id:
                continue
            try:
                detail = self.get_product(product_id)
            except requests.HTTPError:
                continue
            if detail.get("productText"):
                return detail
        return None

    def get_gridpoint_forecast(self, points: dict) -> list[dict]:
        """
        GET the gridpoint /forecast endpoint (URL comes from the /points
        response) and return the list of forecast periods. Each period carries
        a `detailedForecast` text field used as a narrative source.
        """
        forecast_url = points.get("forecast")
        if not forecast_url:
            return []
        resp = self._session.get(forecast_url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get("properties", {}).get("periods", []) or []

    # ------------------------------------------------------------------
    # Normalization -> document records
    # ------------------------------------------------------------------
    def _normalize_alert(self, alert: dict, location: str) -> dict:
        """Normalize one NWS alert into a weather_documents record."""
        narrative = "\n\n".join(
            part
            for part in (alert.get("description"), alert.get("instruction"))
            if part
        ).strip()

        return {
            "id": str(alert.get("id") or ""),
            "location": location,
            "source_type": "alert",
            "headline": alert.get("event") or alert.get("headline") or "Weather Alert",
            "narrative_text": narrative or (alert.get("headline") or ""),
            "effective_at": alert.get("effective") or alert.get("sent"),
            "payload": alert,
            "synced_at": _utcnow(),
        }

    def _normalize_discussion(self, product: dict, location: str, office: str) -> dict:
        """Normalize one NWS Area Forecast Discussion into a weather_documents record."""
        issuance = product.get("issuanceTime", "")
        dedup_key = hashlib.md5(f"{location}|{issuance}".encode("utf-8")).hexdigest()

        return {
            "id": dedup_key,
            "location": location,
            "source_type": "forecast",
            "headline": f"Area Forecast Discussion - {office}",
            "narrative_text": product.get("productText") or "",
            "effective_at": issuance,
            "payload": product,
            "synced_at": _utcnow(),
        }

    def _normalize_forecast_period(self, period: dict, location: str) -> dict:
        """Normalize one gridpoint forecast period into a weather_documents record."""
        start = period.get("startTime", "")
        dedup_key = hashlib.md5(f"period|{location}|{start}".encode("utf-8")).hexdigest()

        return {
            "id": dedup_key,
            "location": location,
            "source_type": "forecast",
            "headline": period.get("name") or "Forecast",
            "narrative_text": period.get("detailedForecast") or "",
            "effective_at": start,
            "payload": period,
            "synced_at": _utcnow(),
        }

    def fetch_documents(self, locations: list[str], limit: int = 50) -> list[dict]:
        """
        Fetch + normalize alerts, forecast discussions, and gridpoint forecast
        periods for each location.

        Returns a flat list of document records ready to upsert into
        `weather_documents`. `limit` caps the number of documents returned per
        location (alerts and forecast periods are capped at `limit` each;
        discussions at 1 each).
        """
        documents: list[dict] = []

        for location in locations:
            location = location.strip()
            if not location:
                continue

            try:
                lat, lon = self.resolve(location)
                points = self.get_points(lat, lon)
                office = points.get("forecastOffice", "").rstrip("/").rsplit("/", 1)[-1]
            except (requests.HTTPError, ValueError, KeyError) as exc:
                print(f"Skipping {location!r}: failed to resolve/fetch points ({exc})")
                continue

            # Active alerts for this point
            try:
                alerts = self.get_active_alerts(lat, lon)
                for alert in alerts[:limit]:
                    doc = self._normalize_alert(alert, location)
                    if doc["id"] and doc["narrative_text"]:
                        documents.append(doc)
            except requests.HTTPError as exc:
                print(f"Skipping alerts for {location!r}: {exc}")

            # Latest Area Forecast Discussion for the forecast office
            try:
                discussion = self.get_forecast_discussion(office)
                if discussion:
                    doc = self._normalize_discussion(discussion, location, office)
                    if doc["narrative_text"]:
                        documents.append(doc)
            except requests.HTTPError as exc:
                print(f"Skipping forecast discussion for {location!r}: {exc}")

            # Gridpoint forecast periods (detailedForecast text)
            try:
                periods = self.get_gridpoint_forecast(points)
                for period in periods[:limit]:
                    doc = self._normalize_forecast_period(period, location)
                    if doc["narrative_text"]:
                        documents.append(doc)
            except requests.HTTPError as exc:
                print(f"Skipping gridpoint forecast for {location!r}: {exc}")

            # Be polite to the NWS API between locations.
            time.sleep(0.5)

        return documents
