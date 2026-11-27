#!/usr/bin/env python3
"""Model catalogue: parsing, validation and resolution order."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tickets"))

from catalog import (  # noqa: E402
    CatalogError,
    find_provider,
    load_catalog,
    load_catalog_cached,
    resolve_selection,
    resolve_ticket,
    usable_providers,
)

CONFIG = {
    "models": {
        "fast": {"provider": "pi", "model": "opencode-go/deepseek-v4.1-flash"},
        "alt": {"provider": "opencode", "model": "opencode/big-pickle"},
    },
    "defaults": {"profile": "fast"},
}


def provider_doc(instance: str, enabled: bool, models: list[dict]) -> dict:
    return {
        "instanceId": instance,
        "displayName": instance.title(),
        "driver": instance,
        "enabled": enabled,
        "installed": enabled,
        "status": "ready" if enabled else "disabled",
        "checkedAt": "2026-11-26T16:34:25.065Z",
        "models": models,
    }


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="catalog-"))
        (self.dir / "pi.json").write_text(
            json.dumps(
                provider_doc(
                    "pi", True,
                    [
                        {
                            "slug": "opencode-go/deepseek-v4.1-flash",
                            "name": "DeepSeek V4.1 Flash",
                            "capabilities": {
                                "optionDescriptors": [
                                    {"id": "thinking", "label": "Thinking", "type": "select",
                                     "options": [{"id": "low", "label": "Low"},
                                                 {"id": "max", "label": "Max", "isDefault": True}]}
                                ]
                            },
                        },
                        {"slug": "opencode-go/qwen3.8-max", "name": "Qwen3.8 Max"},
                    ],
                )
            )
        )
        (self.dir / "claudeAgent.json").write_text(
            json.dumps(provider_doc("claudeAgent", False, [{"slug": "claude-opus-5-5", "name": "Opus"}]))
        )
        (self.dir / "broken.json").write_text("{not json")
        self.catalog = load_catalog(self.dir, CONFIG)

    def test_reads_enabled_and_disabled_providers(self) -> None:
        self.assertEqual(len(self.catalog["providers"]), 2)  # broken file ignored
        pi = find_provider(self.catalog, "pi")
        self.assertTrue(pi["enabled"])
        self.assertEqual(len(pi["models"]), 2)
        self.assertEqual(pi["models"][0]["name"], "DeepSeek V4.1 Flash")

    def test_options_are_parsed(self) -> None:
        pi = find_provider(self.catalog, "pi")
        model = next(m for m in pi["models"] if m["slug"].endswith("deepseek-v4.1-flash"))
        self.assertEqual(model["options"][0]["id"], "thinking")
        values = [v["id"] for v in model["options"][0]["values"]]
        self.assertEqual(values, ["low", "max"])

    def test_usable_providers_filters_disabled(self) -> None:
        names = [p["instanceId"] for p in usable_providers(self.catalog)]
        self.assertEqual(names, ["pi"])

    def test_resolve_selection_accepts_known_model(self) -> None:
        selection = resolve_selection(self.catalog, "pi", "opencode-go/qwen3.8-max")
        self.assertEqual(selection["provider"], "pi")

    def test_resolve_selection_rejects_unknown_provider_model_and_disabled(self) -> None:
        for provider, model in (
            ("nope", "x"),
            ("pi", "no-such-model"),
            ("claudeAgent", "claude-opus-5-5"),
        ):
            with self.subTest(provider=provider, model=model):
                with self.assertRaises(CatalogError):
                    resolve_selection(self.catalog, provider, model)

    def test_ticket_explicit_choice_wins_over_profile(self) -> None:
        ticket = {"id": "T-1", "provider": "pi", "model": "opencode-go/qwen3.8-max",
                  "model_profile": "fast"}
        selection = resolve_ticket(ticket, CONFIG, self.catalog)
        self.assertEqual(selection["model"], "opencode-go/qwen3.8-max")
        self.assertEqual(selection["source"], "ticket")

    def test_profile_used_when_ticket_has_no_choice(self) -> None:
        selection = resolve_ticket({"id": "T-1", "model_profile": "alt"}, CONFIG, self.catalog)
        self.assertEqual(selection["provider"], "opencode")
        self.assertEqual(selection["source"], "profile:alt")

    def test_default_profile_used_last(self) -> None:
        selection = resolve_ticket({"id": "T-1"}, CONFIG, self.catalog)
        self.assertEqual(selection["provider"], "pi")
        self.assertEqual(selection["source"], "default:fast")

    def test_half_a_choice_is_an_error(self) -> None:
        with self.assertRaises(CatalogError):
            resolve_ticket({"id": "T-1", "provider": "pi"}, CONFIG, self.catalog)

    def test_unknown_profile_is_an_error(self) -> None:
        config = json.loads(json.dumps(CONFIG))
        config["defaults"] = {}
        with self.assertRaises(CatalogError):
            resolve_ticket({"id": "T-1", "model_profile": "hilang"}, config, self.catalog)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CatalogueCacheTests(unittest.TestCase):
    """The board asks for the catalogue on every dialog open; reading the files each
    time is wasted work, but a stale answer is worse. Cache on the directory's mtime."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="catalog-cache-"))
        (self.tmp / "pi.json").write_text(json.dumps(
            provider_doc("pi", True, [{"slug": "fast-model", "name": "Fast"}])))

    def test_same_snapshot_is_served_from_cache(self) -> None:
        first = load_catalog_cached(self.tmp, CONFIG)
        second = load_catalog_cached(self.tmp, CONFIG)
        self.assertIs(first, second)

    def test_a_new_provider_file_invalidates_the_cache(self) -> None:
        first = load_catalog_cached(self.tmp, CONFIG)
        self.assertEqual([p["instanceId"] for p in first["providers"]], ["pi"])
        (self.tmp / "opencode.json").write_text(json.dumps(
            provider_doc("opencode", True, [{"slug": "build-model", "name": "Build"}])))
        second = load_catalog_cached(self.tmp, CONFIG)
        self.assertEqual(sorted(p["instanceId"] for p in second["providers"]), ["opencode", "pi"])
