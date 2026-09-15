from __future__ import annotations

import json
import unittest
from html import escape
from unittest.mock import patch

from astrbot_plugin_alice_image_assistant.alice_image.reverse.constant import (
    SOURCE_KEY_YANDEX,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.yandex_strategy import (
    YandexStrategy,
)


class _FakeResponse:
    def __init__(self, body: str, status: int = 200) -> None:
        self.body = body
        self.status = status

    async def text(self, **_kwargs: object) -> str:
        return self.body

    async def read(self) -> bytes:
        return self.body.encode()


class _FakeRequest:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _FakeResponse:
        return self.response

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def get(self, *args: object, **kwargs: object) -> _FakeRequest:
        self.calls.append((args, kwargs))
        response = self.responses.pop(0)
        return _FakeRequest(response)


def _page(sites: object) -> str:
    state = {"initialState": {"cbirSites": {"sites": sites}}}
    encoded = escape(json.dumps(state, ensure_ascii=False), quote=True)
    return f'<div class="Root" id="ImagesApp-test" data-state="{encoded}"></div>'


def _site(index: int, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "url": f"https://example.com/art/{index}",
        "title": f"作品 {index}",
        "description": f"描述 {index}",
        "domain": "example.com",
        "thumb": {"url": f"//cdn.example.com/{index}.jpg"},
        "originalImage": {"width": 1200, "height": 800},
    }
    value.update(overrides)
    return value


def test_data_state_is_parsed_and_protocol_relative_thumbnail_is_normalized() -> None:
    results = YandexStrategy._parse_html(_page([_site(1)]))

    assert len(results) == 1
    item = results[0]
    assert item.source == "Yandex"
    assert item.source_key == SOURCE_KEY_YANDEX
    assert item.url == "https://example.com/art/1"
    assert item.thumbnail == "https://cdn.example.com/1.jpg"
    assert item.domain == "example.com"
    assert item.description == "描述 1"
    assert item.score is not None


def test_missing_state_and_captcha_are_empty() -> None:
    assert YandexStrategy._parse_html("<html><body>captcha</body></html>") == []
    assert YandexStrategy._parse_html("<html><body>no result</body></html>") == []


def test_dirty_sites_are_skipped_and_max_results_is_applied() -> None:
    sites: list[object] = [
        {"url": "javascript:alert(1)"},
        _site(1),
        {"url": "https://example.com/art/1", "title": "duplicate"},
        _site(2),
        _site(3),
    ]
    results = YandexStrategy(max_results="2")._parse_html(_page(sites), max_results=2)

    assert [item.url for item in results] == [
        "https://example.com/art/1",
        "https://example.com/art/2",
    ]


class YandexAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_has_imageview_params_and_cookie_is_not_logged(self) -> None:
        session = _FakeSession([_FakeResponse(_page([_site(1)]))])
        strategy = YandexStrategy(cookies="session=secret-cookie; foo=bar")

        with patch(
            "astrbot_plugin_alice_image_assistant.alice_image.reverse.yandex_strategy.get_aiohttp_session",
            return_value=session,
        ):
            results = await strategy.search("https://input.example/image.jpg")

        self.assertEqual(len(results), 1)
        self.assertEqual(len(session.calls), 1)
        args, kwargs = session.calls[0]
        self.assertEqual(args[0], "https://yandex.com/images/search")
        self.assertEqual(
            kwargs["params"],
            {"rpt": "imageview", "url": "https://input.example/image.jpg"},
        )
        self.assertEqual(
            kwargs["cookies"], {"session": "secret-cookie", "foo": "bar"}
        )

    async def test_com_failure_falls_back_to_ru(self) -> None:
        session = _FakeSession(
            [
                _FakeResponse("blocked", status=403),
                _FakeResponse(_page([_site(2)])),
            ]
        )
        strategy = YandexStrategy(use_ru_fallback="true")

        with patch(
            "astrbot_plugin_alice_image_assistant.alice_image.reverse.yandex_strategy.get_aiohttp_session",
            return_value=session,
        ):
            results = await strategy.search("https://input.example/image.jpg")

        self.assertEqual([item.url for item in results], ["https://example.com/art/2"])
        self.assertEqual(
            [call[0][0] for call in session.calls],
            [
                "https://yandex.com/images/search",
                "https://yandex.ru/images/search",
            ],
        )

    async def test_exception_text_is_redacted_from_logs(self) -> None:
        class _BrokenSession:
            def get(self, *_args: object, **_kwargs: object) -> _FakeRequest:
                raise RuntimeError("request failed with secret-cookie")

        strategy = YandexStrategy(cookies="session=secret-cookie")
        with patch(
            "astrbot_plugin_alice_image_assistant.alice_image.reverse.yandex_strategy.get_aiohttp_session",
            return_value=_BrokenSession(),
        ), patch(
            "astrbot_plugin_alice_image_assistant.alice_image.reverse.yandex_strategy.logger.warning"
        ) as warning:
            self.assertEqual(
                await strategy.search("https://input.example/image.jpg"), []
            )

        messages = " ".join(str(call.args[0]) for call in warning.call_args_list)
        self.assertNotIn("secret-cookie", messages)


def test_invalid_config_values_do_not_crash() -> None:
    strategy = YandexStrategy(max_results="not-a-number", use_ru_fallback="off")

    assert strategy.max_results == 5
    assert strategy.use_ru_fallback is False
