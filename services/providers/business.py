"""Validated enterprise imports. No sample records or fallback quantities."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Protocol

import requests

from config.settings import settings
from core.schemas.contracts import (
    BusinessInputs,
    BusinessSnapshotIdentity,
    ProviderEnvelope,
    Quality,
    utcnow,
)


class BusinessProvider(Protocol):
    def fetch(self) -> ProviderEnvelope[BusinessInputs]: ...


def production_source_errors(data=None, provider=None):
    if settings.ENVIRONMENT != "production":
        return []
    if (
        settings.BUSINESS_SOURCE_TRUST != "AUTHORITATIVE"
        or not settings.BUSINESS_SOURCE_ID
    ):
        return ["BUSINESS_SOURCE_NOT_AUTHORITATIVE"]
    labels = [settings.BUSINESS_SOURCE_ID]
    if provider is not None:
        labels.extend(
            str(getattr(provider, key, "")) for key in ("path", "directory", "url")
        )
    if data is not None:
        for domain in type(data).model_fields:
            records = getattr(data, domain)
            for record in records if isinstance(records, list) else [records]:
                if record is not None:
                    labels.append(record.source)
                    labels.extend(p.source for p in record.provenance)
    pattern = r"(^|[^a-z])(mock|demo|synthetic|fixture|simulation|test)([^a-z]|$)"
    return (
        ["BUSINESS_MOCK_SOURCE_FORBIDDEN"]
        if any(re.search(pattern, label.lower()) for label in labels)
        else []
    )


def envelope(data: BusinessInputs, source: str) -> ProviderEnvelope[BusinessInputs]:
    forbidden = production_source_errors(data)
    if forbidden:
        return ProviderEnvelope[BusinessInputs](
            source=source,
            entity_id="enterprise",
            quality=Quality.INVALID,
            errors=forbidden,
        )
    records = [
        r
        for name in type(data).model_fields
        for r in (
            getattr(data, name)
            if isinstance(getattr(data, name), list)
            else [getattr(data, name)]
        )
        if r
    ]
    quality = Quality.UNAVAILABLE
    if records:
        quality = Quality.VALID
        if any(
            r.observed_at > utcnow() or r.data_quality == Quality.INVALID
            for r in records
        ):
            quality = Quality.INVALID
        elif any(
            r.valid_until < utcnow()
            or r.data_quality == Quality.STALE
            or (utcnow() - r.observed_at).total_seconds()
            > settings.BUSINESS_MAX_AGE_SECONDS
            for r in records
        ):
            quality = Quality.STALE
        elif any(r.data_quality != Quality.VALID for r in records):
            quality = Quality.PARTIAL
    return ProviderEnvelope[BusinessInputs](
        source=source,
        entity_id="enterprise",
        observed_at=min((r.observed_at for r in records), default=None),
        valid_until=min((r.valid_until for r in records), default=None),
        quality=quality,
        payload=data,
        errors=[] if records else ["BUSINESS_DATA_UNAVAILABLE"],
    )


class UnavailableBusinessProvider:
    def fetch(self):
        return ProviderEnvelope[BusinessInputs](
            source="unconfigured",
            entity_id="enterprise",
            quality=Quality.UNAVAILABLE,
            errors=["BUSINESS_SOURCE_NOT_CONFIGURED"],
        )


class JSONBusinessProvider:
    def __init__(self, path: Path):
        self.path = path

    def fetch(self):
        before = self.path.stat()
        if before.st_size > 20_000_000:
            raise ValueError("enterprise import exceeds size limit")
        content = self.path.read_text(encoding="utf-8-sig")
        after = self.path.stat()
        if (before.st_mtime_ns, before.st_size, before.st_ino) != (
            after.st_mtime_ns,
            after.st_size,
            after.st_ino,
        ):
            raise ValueError("BUSINESS_SOURCE_CHANGED_DURING_CAPTURE")
        return envelope(
            BusinessInputs.model_validate_json(content),
            "json-import",
        )


class CSVBusinessProvider:
    """A directory of domain-named CSV files; nested provenance is JSON.

    No metadata is invented by the adapter. Each row carries the canonical
    entity fields. Navigation networks use route_network.json.
    """

    def __init__(self, directory: Path):
        self.directory = directory

    def fetch(self):
        payload = {}
        files = sorted(self.directory.glob("*.csv")) + sorted(
            self.directory.glob("route_network.json")
        )
        before = {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in files}
        if any(size > 20_000_000 for _, size in before.values()):
            raise ValueError("enterprise import exceeds size limit")
        for domain in BusinessInputs.model_fields:
            if domain == "route_network":
                path = self.directory / "route_network.json"
                if path.exists():
                    payload[domain] = json.loads(path.read_text(encoding="utf-8-sig"))
                continue
            path = self.directory / f"{domain}.csv"
            if not path.exists():
                continue
            if path.stat().st_size > 20_000_000:
                raise ValueError("enterprise import exceeds size limit")
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = []
                for row in csv.DictReader(stream):
                    item = {k: v for k, v in row.items() if v != ""}
                    for key, value in list(item.items()):
                        if value.startswith(("[", "{")):
                            item[key] = json.loads(value)
                    rows.append(item)
                payload[domain] = rows
        after_files = sorted(self.directory.glob("*.csv")) + sorted(
            self.directory.glob("route_network.json")
        )
        after = {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in after_files}
        if before != after:
            raise ValueError("BUSINESS_SOURCE_CHANGED_DURING_CAPTURE")
        return envelope(BusinessInputs.model_validate(payload), "csv-import")


class APIBusinessProvider:
    def __init__(self, url: str, token: str):
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("enterprise API requires HTTPS")
        self.url, self.token = url, token

    def fetch(self):
        response = requests.get(
            self.url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=settings.PROVIDER_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise ValueError("BUSINESS_API_REDIRECT_REJECTED")
        return envelope(
            BusinessInputs.model_validate(response.json()), "enterprise-api"
        )


def configured_business_provider() -> BusinessProvider:
    if settings.BUSINESS_API_URL:
        return APIBusinessProvider(
            settings.BUSINESS_API_URL, settings.BUSINESS_API_TOKEN.get_secret_value()
        )
    if settings.BUSINESS_DATA_PATH:
        path = Path(settings.BUSINESS_DATA_PATH)
        return (
            CSVBusinessProvider(path) if path.is_dir() else JSONBusinessProvider(path)
        )
    return UnavailableBusinessProvider()


def business_hash(data: BusinessInputs) -> str:
    # Collection order is immaterial; identities, versions and every fact are bound.
    payload = data.model_dump(mode="json")
    for name, value in payload.items():
        if isinstance(value, list):
            payload[name] = sorted(value, key=lambda item: item["id"])
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def source_id(provider) -> str:
    location = (
        getattr(provider, "url", None)
        or getattr(provider, "path", None)
        or getattr(provider, "directory", None)
    )
    if location is None:
        return "request-import" if provider is None else "unconfigured"
    return (
        settings.BUSINESS_SOURCE_ID
        or hashlib.sha256(str(location).encode()).hexdigest()
    )


def snapshot_identity(data, source, provider=None):
    return BusinessSnapshotIdentity(
        source_id=source_id(provider),
        adapter=source,
        snapshot_hash=business_hash(data),
        versions={
            f"{domain}:{item.id}": item.version
            for domain in type(data).model_fields
            for item in (
                getattr(data, domain)
                if isinstance(getattr(data, domain), list)
                else [getattr(data, domain)]
            )
            if item is not None
        },
        configured=provider is not None
        and not isinstance(provider, UnavailableBusinessProvider),
    )


def fetch_business(provider):
    if not isinstance(provider, UnavailableBusinessProvider):
        forbidden = production_source_errors(provider=provider)
        if forbidden:
            return ProviderEnvelope[BusinessInputs](
                source=type(provider).__name__,
                entity_id="enterprise",
                quality=Quality.INVALID,
                errors=forbidden,
            )
    try:
        return provider.fetch()
    except Exception as exc:
        return ProviderEnvelope[BusinessInputs](
            source=type(provider).__name__,
            entity_id="enterprise",
            quality=Quality.UNAVAILABLE,
            errors=[f"BUSINESS_SOURCE_ERROR:{type(exc).__name__}"],
        )


def verify_source(state, *, require_configured=False):
    """Re-read configured evidence; never replace a captured/approved snapshot."""
    identity = state.get("data", {}).get("business_identity")
    if not identity:
        raise ValueError("BUSINESS_SNAPSHOT_IDENTITY_REQUIRED")
    identity = BusinessSnapshotIdentity.model_validate(identity)
    captured = BusinessInputs.model_validate(state["business_inputs"])
    if production_source_errors(captured):
        raise ValueError("AUTHORITATIVE_BUSINESS_SOURCE_REQUIRED")
    if settings.ENVIRONMENT == "production":
        require_configured = True
    if business_hash(captured) != identity.snapshot_hash:
        raise ValueError("BUSINESS_SNAPSHOT_HASH_MISMATCH")
    if not identity.configured and not require_configured:
        return
    provider = configured_business_provider()
    current = fetch_business(provider)
    if current.quality != Quality.VALID or current.payload is None:
        raise ValueError("AUTHORITATIVE_BUSINESS_SOURCE_UNAVAILABLE")
    if identity.configured and source_id(provider) != identity.source_id:
        raise ValueError("BUSINESS_SOURCE_IDENTITY_CHANGED")
    if business_hash(current.payload) != identity.snapshot_hash:
        raise ValueError("BUSINESS_SOURCE_CHANGED_NEW_RUN_REQUIRED")
