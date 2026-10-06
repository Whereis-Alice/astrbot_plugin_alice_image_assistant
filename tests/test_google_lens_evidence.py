from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from astrbot_plugin_alice_image_assistant.alice_image.reverse import google_lens_strategy as lens


class Response:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    async def text(self):
        return json.dumps(self.data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


def match(name):
    return {
        "title": name,
        "link": f"https://example.com/{name}",
        "source": "page",
        "snippet": "specific clue",
    }


class GoogleLensEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def search(self, data, **kwargs):
        session = Session([Response(data)])
        with patch.object(lens, "get_aiohttp_session", AsyncMock(return_value=session)):
            results = await lens.GoogleLensStrategy(api_keys=["test"], **kwargs).search(
                "https://example.com/query.jpg"
            )
        return results, session

    async def test_exact_matches_and_visual_matches_keep_metadata(self):
        results, _ = await self.search(
            {"exact_matches": [match("exact")], "visual_matches": [match("similar")]}
        )
        self.assertEqual([r.title for r in results], ["exact", "similar"])
        self.assertEqual(results[0].source_key, "google_lens/exact")
        self.assertIsNone(results[0].similarity)
        self.assertEqual(results[1].domain, "example.com")
        self.assertIn("specific clue", results[1].description)

    async def test_invalid_items_do_not_consume_result_slots(self):
        results, _ = await self.search(
            {
                "visual_matches": [
                    None,
                    {},
                    {"title": "bad", "link": "javascript:x"},
                    match("a"),
                    match("b"),
                ]
            },
            max_results=2,
        )
        self.assertEqual([r.title for r in results], ["a", "b"])

    async def test_explicit_type_language_country_and_crop_are_sent(self):
        results, session = await self.search(
            {"exact_matches": [match("exact")], "visual_matches": [match("similar")]},
            search_type="exact_matches",
            language="ja",
            country="jp",
            auto_crop=True,
        )
        self.assertEqual([r.title for r in results], ["exact"])
        params = parse_qs(urlsplit(session.urls[0]).query)
        self.assertEqual(params["type"], ["exact_matches"])
        self.assertEqual(params["hl"], ["ja"])
        self.assertEqual(params["country"], ["jp"])
        self.assertEqual(params["auto_crop"], ["true"])

    async def test_total_results_respect_configured_limit(self):
        results, _ = await self.search(
            {"exact_matches": [match("exact")], "visual_matches": [match("a"), match("b")]},
            max_results=2,
        )
        self.assertEqual(len(results), 2)

    async def test_rate_limit_rotates_key(self):
        session = Session([Response({}, 429), Response({"visual_matches": [match("ok")]})])
        strategy = lens.GoogleLensStrategy(api_keys=["first", "second"])
        with patch.object(lens, "get_aiohttp_session", AsyncMock(return_value=session)):
            results = await strategy.search("https://example.com/query.jpg")
        self.assertEqual(results[0].title, "ok")
        self.assertEqual(
            [parse_qs(urlsplit(url).query)["api_key"][0] for url in session.urls],
            ["first", "second"],
        )

    async def test_duplicate_links_do_not_hide_later_specific_leads(self):
        duplicate = {**match("exact"), "title": "alternate page title"}
        results, _ = await self.search(
            {
                "exact_matches": [match("exact")],
                "visual_matches": [duplicate, duplicate, match("specific")],
            },
            max_results=2,
        )
        self.assertEqual([r.title for r in results], ["exact", "specific"])
        self.assertEqual(results[0].source_key, "google_lens/exact")
        self.assertEqual(len(results[0].evidence), 2)

    async def test_no_results_is_distinct_from_http_failure(self):
        results, _ = await self.search(
            {"error": "Google Lens hasn't returned any results for this query."}
        )
        self.assertEqual(results, [])
        session = Session([Response({}, 503)])
        with (
            patch.object(lens, "get_aiohttp_session", AsyncMock(return_value=session)),
            self.assertRaisesRegex(RuntimeError, "不可用"),
        ):
            await lens.GoogleLensStrategy(api_keys=["test"]).search("https://example.com/query.jpg")
