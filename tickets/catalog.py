#!/usr/bin/env python3
"""Model catalogue read from T3's own provider caches.

T3 writes one JSON file per provider instance under `~/.t3/caches/<instanceId>.json`
while it probes the harnesses. Each file carries:

    instanceId, displayName, driver, enabled, installed, status, checkedAt,
    models: [{ slug, name, subProvider, isDefault, capabilities: { optionDescriptors: [...] } }]

That is the same data the T3 UI shows, which makes it the right source for "which
harnesses and models can I actually pick": no hard-coded list, no guessing.

What this module does *not* do: apply model options. The `t3 thread start` CLI has no
flag for `thinking`/`variant`/`effort`, so options are reported for information and the
thread keeps whatever the project default is. Changing that needs a T3 feature, not a
workaround here.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
CATALOG_DIR = Path(os.environ.get("T3_CATALOG_DIR", HOME / ".t3/caches"))


class CatalogError(Exception):
    """A selection that cannot be honoured, with a message for the operator."""


def _read_provider(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "models" not in data:
        return None
    models = []
    for model in data.get("models") or []:
        descriptors = ((model.get("capabilities") or {}).get("optionDescriptors")) or []
        models.append(
            {
                "slug": model.get("slug", ""),
                "name": model.get("name") or model.get("slug", ""),
                "subProvider": model.get("subProvider", ""),
                "isDefault": bool(model.get("isDefault")),
                "options": [
                    {
                        "id": descriptor.get("id", ""),
                        "label": descriptor.get("label", descriptor.get("id", "")),
                        "type": descriptor.get("type", "select"),
                        "values": [
                            {
                                "id": value.get("id", ""),
                                "label": value.get("label", value.get("id", "")),
                                "isDefault": bool(value.get("isDefault")),
                            }
                            for value in descriptor.get("options") or []
                        ],
                    }
                    for descriptor in descriptors
                ],
            }
        )
    return {
        "instanceId": data.get("instanceId") or path.stem,
        "displayName": data.get("displayName") or path.stem,
        "driver": data.get("driver", ""),
        "enabled": bool(data.get("enabled")),
        "installed": bool(data.get("installed")),
        "status": data.get("status", ""),
        "checkedAt": data.get("checkedAt", ""),
        "models": sorted(models, key=lambda item: item["name"].lower()),
    }


def load_catalog(directory: Path | None = None, config: dict | None = None) -> dict:
    """Providers, their models, the configured presets and the default choice."""
    directory = Path(directory or CATALOG_DIR)
    providers = []
    if directory.exists():
        for path in sorted(directory.glob("*.json")):
            provider = _read_provider(path)
            if provider:
                providers.append(provider)
    providers.sort(key=lambda item: item["displayName"].lower())

    presets = []
    default_profile = ""
    if config:
        models = config.get("models") or {}
        for name in sorted(models):
            presets.append(
                {
                    "name": name,
                    "provider": models[name].get("provider", ""),
                    "model": models[name].get("model", ""),
                }
            )
        default_profile = (config.get("defaults") or {}).get("profile", "")

    return {
        "providers": providers,
        "presets": presets,
        "defaultProfile": default_profile,
        "source": str(directory),
        "loadedAt": datetime.now(timezone.utc).isoformat(),
    }


_CACHE: dict[tuple, dict] = {}
_CACHE_LIMIT = 8


def _snapshot_key(directory: Path, config: dict | None) -> tuple:
    """Cheap identity of the inputs: file names with mtime and size, plus the presets."""
    files = []
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        entries = []
    for path in entries:
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((path.name, stat.st_mtime_ns, stat.st_size))
    presets = json.dumps(
        {key: (config or {}).get(key) for key in ("models", "defaults")}, sort_keys=True
    )
    return (str(directory), tuple(files), presets)


def load_catalog_cached(directory: Path | None = None, config: dict | None = None) -> dict:
    """The same payload as `load_catalog`, re-read only when its inputs change.

    The board asks for the catalogue on every page load and every dialog open; re-reading
    a few hundred kilobytes of JSON each time is waste, while a stale answer is worse. The
    key is the directory's file list with mtime and size, so a provider probe that rewrites
    a cache file invalidates it. The payload itself contains `loadedAt`, which therefore
    means "when this snapshot was read", not "when you asked".
    """
    directory = Path(directory or CATALOG_DIR)
    key = _snapshot_key(directory, config)
    cached = _CACHE.get(key)
    if cached is None:
        cached = load_catalog(directory, config=config)
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.clear()
        _CACHE[key] = cached
    return cached


def usable_providers(catalog: dict) -> list[dict]:
    return [p for p in catalog["providers"] if p["enabled"] and p["installed"]]


def find_provider(catalog: dict, instance_id: str) -> dict | None:
    return next((p for p in catalog["providers"] if p["instanceId"] == instance_id), None)


def resolve_selection(catalog: dict, provider_id: str, model_slug: str) -> dict:
    """Validate an explicit harness/model pair against the live catalogue."""
    if not provider_id:
        raise CatalogError("provider is empty")
    if not model_slug:
        raise CatalogError("model is empty")
    provider = find_provider(catalog, provider_id)
    if provider is None:
        known = ", ".join(p["instanceId"] for p in catalog["providers"]) or "none"
        raise CatalogError(f"unknown provider '{provider_id}' (available: {known})")
    if not (provider["enabled"] and provider["installed"]):
        raise CatalogError(f"provider '{provider_id}' is not enabled in T3")
    if not any(m["slug"] == model_slug for m in provider["models"]):
        raise CatalogError(f"model '{model_slug}' is not offered by provider '{provider_id}'")
    return {"provider": provider_id, "model": model_slug}


def resolve_ticket(ticket: dict, config: dict, catalog: dict | None = None) -> dict:
    """Where the provider and model for a ticket come from, in order.

    1. `provider` + `model` stored on the ticket (validated against the catalogue),
    2. the ticket's `model_profile`,
    3. `defaults.profile` from config.

    Returns {"provider", "model", "source"} where source is ticket, profile:<name>,
    or default:<name>.
    """
    catalog = catalog or load_catalog(config=config)
    provider, model = (ticket.get("provider") or "").strip(), (ticket.get("model") or "").strip()
    if provider or model:
        if not provider or not model:
            raise CatalogError(
                f"{ticket['id']}: provider and model must be set together, "
                "or leave both empty and use a preset"
            )
        selection = resolve_selection(catalog, provider, model)
        return {**selection, "source": "ticket"}

    profile_name = (ticket.get("model_profile") or "").strip() or (
        (config.get("defaults") or {}).get("profile") or ""
    )
    if not profile_name:
        raise CatalogError(
            f"{ticket['id']}: no provider/model and no default profile in config"
        )
    profile = (config.get("models") or {}).get(profile_name)
    if not profile:
        available = ", ".join(sorted((config.get("models") or {}))) or "none"
        raise CatalogError(f"model profile '{profile_name}' is not in config (available: {available})")
    if not profile.get("provider") or not profile.get("model"):
        raise CatalogError(f"profile '{profile_name}' must define provider and model")
    source = "profile" if (ticket.get("model_profile") or "").strip() else "default"
    return {
        "provider": profile["provider"],
        "model": profile["model"],
        "source": f"{source}:{profile_name}",
    }
