from __future__ import annotations

import unittest
from types import SimpleNamespace

from astrbot_plugin_alice_image_assistant.alice_image.forward.orchestrator import (
    ForwardSearchOrchestrator,
)


class _Event:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, result) -> None:
        self.sent.append(result)

    @staticmethod
    def chain_result(chain):
        return chain


class _Pixiv:
    def __init__(self, success: bool) -> None:
        self.success = success
        self.calls = 0

    async def search(self, *_args, **_kwargs):
        self.calls += 1
        return SimpleNamespace(
            success=self.success,
            error="pixiv failed" if not self.success else "",
            sent_count=1 if self.success else 0,
            review_fallback=False,
            ids=[123] if self.success else [],
        )

    async def close(self) -> None:
        return None


class _Soutu:
    def __init__(self, success: bool, review_fallback: bool = False) -> None:
        self.success = success
        self.review_fallback = review_fallback
        self.calls = 0

    async def search(self, *_args, **_kwargs):
        self.calls += 1
        if self.success:
            return b"image", "", self.review_fallback
        return None, "soutu failed", False

    async def terminate(self) -> None:
        return None


class _Serp:
    def __init__(self, success: bool, review_fallback: bool = False) -> None:
        self.success = success
        self.review_fallback = review_fallback
        self.calls = 0
        self.last_kwargs = {}

    def available(self) -> bool:
        return True

    async def search(self, *_args, **_kwargs):
        self.calls += 1
        self.last_kwargs = _kwargs
        return SimpleNamespace(
            image_bytes=b"image" if self.success else None,
            error="" if self.success else "serp failed",
            review_fallback=self.review_fallback,
        )

    async def close(self) -> None:
        return None


def _config(**overrides):
    value = {
        "fallback_enabled": True,
        "fallback_order": ["pixiv", "soutu", "serpapi"],
        "auto_source_enabled": True,
        "tool_send_images": True,
        "pixiv": {"enabled": True},
        "soutu": {"enabled": True, "vlm_selection_enabled": True},
        "serpapi": {"enabled": True},
        "llm_review": {"enabled": True, "commands_enabled": True},
    }
    value.update(overrides)
    return value


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_prefers_pixiv_for_anime_queries(self) -> None:
        pixiv, soutu, serp = _Pixiv(True), _Soutu(True), _Serp(True)
        service = ForwardSearchOrchestrator(_config(), pixiv, soutu, serp)
        result = await service.search(_Event(), "初音ミク 插画", "初音未来", "auto")
        self.assertTrue(result.success)
        self.assertEqual(result.source, "pixiv")
        self.assertEqual(result.attempted_sources, ["pixiv"])
        self.assertEqual(soutu.calls, 0)

    async def test_explicit_source_falls_back_in_configured_order(self) -> None:
        pixiv, soutu, serp = _Pixiv(False), _Soutu(False), _Serp(True)
        service = ForwardSearchOrchestrator(_config(), pixiv, soutu, serp)
        result = await service.search(_Event(), "雪山", "雪山日出", "pixiv")
        self.assertTrue(result.success)
        self.assertEqual(result.source, "serpapi")
        self.assertEqual(result.attempted_sources, ["pixiv", "soutu", "serpapi"])
        self.assertEqual(set(result.errors), {"pixiv", "soutu"})

    async def test_disabling_fallback_never_calls_next_source(self) -> None:
        pixiv, soutu, serp = _Pixiv(False), _Soutu(True), _Serp(True)
        service = ForwardSearchOrchestrator(
            _config(fallback_enabled=False), pixiv, soutu, serp
        )
        result = await service.search(_Event(), "角色插画", "角色", "pixiv")
        self.assertFalse(result.success)
        self.assertEqual(result.attempted_sources, ["pixiv"])
        self.assertEqual(soutu.calls, 0)
        self.assertEqual(serp.calls, 0)

    def test_disabled_source_is_removed_from_auto_order(self) -> None:
        config = _config(pixiv={"enabled": False})
        service = ForwardSearchOrchestrator(
            config, _Pixiv(True), _Soutu(True), _Serp(True)
        )
        self.assertEqual(service.choose_sources("动漫插画", "auto")[0], "soutu")

    async def test_fail_closed_review_rejects_fallback_image_and_uses_next_source(
        self,
    ) -> None:
        soutu = _Soutu(True, review_fallback=True)
        serp = _Serp(True)
        config = _config(
            pixiv={"enabled": False},
            llm_review={
                "enabled": True,
                "commands_enabled": True,
                "fail_open": False,
            },
        )
        service = ForwardSearchOrchestrator(config, None, soutu, serp)

        result = await service.search(_Event(), "雪山", "雪山日出", "soutu")

        self.assertTrue(result.success)
        self.assertEqual(result.source, "serpapi")
        self.assertIn("不放行首图", result.errors["soutu"])
        self.assertEqual(serp.calls, 1)

    async def test_serpapi_vlm_switch_disables_review_and_fail_closed_check(
        self,
    ) -> None:
        serp = _Serp(True, review_fallback=True)
        config = _config(
            pixiv={"enabled": False},
            soutu={"enabled": False},
            serpapi={"enabled": True, "vlm_selection_enabled": False},
            llm_review={
                "enabled": True,
                "commands_enabled": True,
                "fail_open": False,
            },
        )
        service = ForwardSearchOrchestrator(config, None, None, serp)

        result = await service.search(_Event(), "雪山", "雪山日出", "serpapi")

        self.assertTrue(result.success)
        self.assertEqual(result.source, "serpapi")
        self.assertFalse(serp.last_kwargs["review_enabled"])


if __name__ == "__main__":
    unittest.main()
