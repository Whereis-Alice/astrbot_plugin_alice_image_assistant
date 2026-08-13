from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot_plugin_alice_image_assistant.alice_image.forward.session_review import (
    CurrentSessionReviewProvider,
    SessionReviewResolver,
    _recent_dialogue,
    _remove_leading_dialogue,
)


class SessionReviewTests(unittest.IsolatedAsyncioTestCase):
    def test_recent_dialogue_keeps_only_requested_user_turns(self) -> None:
        messages = [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "回复一"},
            {"role": "user", "content": "第二轮"},
            {"role": "assistant", "content": "回复二"},
            {"role": "tool", "content": "工具结果"},
            {"role": "user", "content": "就是刚才说的那个，找张图"},
        ]

        result = _recent_dialogue(messages, 2)

        self.assertEqual(
            result,
            [
                {"role": "user", "content": "第二轮"},
                {"role": "assistant", "content": "回复二"},
                {"role": "user", "content": "就是刚才说的那个，找张图"},
            ],
        )

    def test_recent_dialogue_drops_orphan_assistant_messages(self) -> None:
        self.assertEqual(
            _recent_dialogue(
                [{"role": "assistant", "content": "没有对应用户请求的回复"}],
                4,
            ),
            [],
        )

    def test_persona_begin_dialogue_is_removed_before_recent_turn_slicing(
        self,
    ) -> None:
        persona = [
            {"role": "user", "content": "预设问题"},
            {"role": "assistant", "content": "预设回答"},
        ]
        dialogue = persona + [
            {"role": "user", "content": "当前请求"},
        ]

        self.assertEqual(
            _remove_leading_dialogue(dialogue, persona),
            [{"role": "user", "content": "当前请求"}],
        )

    async def test_current_provider_uses_live_context_persona_and_no_tools(
        self,
    ) -> None:
        provider = SimpleNamespace(
            text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="{}"))
        )
        conversation = SimpleNamespace(
            persona_id="alice",
            history=json.dumps(
                [
                    {"role": "user", "content": "数据库旧请求"},
                    {"role": "assistant", "content": "数据库旧回复"},
                ]
            ),
        )
        conversation_manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="cid"),
            get_conversation=AsyncMock(return_value=conversation),
        )
        persona_manager = SimpleNamespace(
            resolve_selected_persona=AsyncMock(
                return_value=(
                    "alice",
                    {
                        "prompt": "你是爱丽丝，重视用户刚才表达的偏好。",
                        "_begin_dialogs_processed": [
                            {"role": "user", "content": "你喜欢怎样挑图？"},
                            {"role": "assistant", "content": "我会重视准确性。"},
                        ],
                    },
                    None,
                    False,
                )
            )
        )
        context = SimpleNamespace(
            conversation_manager=conversation_manager,
            persona_manager=persona_manager,
            get_config=Mock(return_value={"provider_settings": {}}),
        )
        event = SimpleNamespace(
            unified_msg_origin="test:group:room",
            get_platform_name=lambda: "test",
        )
        live_context = SimpleNamespace(
            messages=[
                {"role": "system", "content": "完整主 Agent 系统提示"},
                {"role": "user", "content": "我更想看真实照片"},
                {"role": "assistant", "content": "明白"},
                {"role": "user", "content": "给我找刚才说的那个"},
            ]
        )
        reviewer = CurrentSessionReviewProvider(
            provider,
            context,
            event,
            agent_run_context=live_context,
            context_turns=2,
        )

        await reviewer.text_chat(
            prompt="选择海狸图片，只返回 JSON",
            image_urls=["base64://image"],
            func_tool=object(),
            tool_choice="required",
        )

        call = provider.text_chat.await_args.kwargs
        self.assertIsNone(call["func_tool"])
        self.assertNotIn("工具结果", str(call["contexts"]))
        self.assertIn("给我找刚才说的那个", str(call["contexts"]))
        self.assertNotIn("数据库旧请求", str(call["contexts"]))
        self.assertEqual(call["contexts"][0]["content"], "你喜欢怎样挑图？")
        self.assertEqual(
            sum(
                item["content"] == "你喜欢怎样挑图？"
                for item in call["contexts"]
            ),
            1,
        )
        self.assertIn("你是爱丽丝", call["system_prompt"])
        self.assertIn("Do not call tools", call["system_prompt"])
        persona_manager.resolve_selected_persona.assert_awaited_once()
        self.assertEqual(
            persona_manager.resolve_selected_persona.await_args.kwargs[
                "conversation_persona_id"
            ],
            "alice",
        )

    async def test_explicit_reviewer_does_not_inherit_current_session(self) -> None:
        explicit_provider = object()
        current_provider = object()
        context = SimpleNamespace(
            get_provider_by_id=Mock(
                side_effect=lambda provider_id: {
                    "explicit": explicit_provider,
                    "current": current_provider,
                }.get(provider_id)
            ),
            get_current_chat_provider_id=AsyncMock(return_value="current"),
        )
        resolver = SessionReviewResolver(
            context,
            {"current_session_bot_enabled": True},
        )

        result = await resolver.resolve(
            SimpleNamespace(unified_msg_origin="test:group:room"),
            "explicit",
            agent_run_context=SimpleNamespace(messages=[]),
        )

        self.assertIs(result, explicit_provider)
        context.get_current_chat_provider_id.assert_not_awaited()

    async def test_switch_off_returns_plain_current_provider(self) -> None:
        provider = object()
        context = SimpleNamespace(
            get_provider_by_id=Mock(return_value=provider),
            get_current_chat_provider_id=AsyncMock(return_value="current"),
        )
        resolver = SessionReviewResolver(
            context,
            {"current_session_bot_enabled": False},
        )

        result = await resolver.resolve(
            SimpleNamespace(unified_msg_origin="test:group:room")
        )

        self.assertIs(result, provider)

    async def test_missing_explicit_reviewer_fallback_stays_session_independent(
        self,
    ) -> None:
        current_provider = object()
        context = SimpleNamespace(
            get_provider_by_id=Mock(
                side_effect=lambda provider_id: (
                    current_provider if provider_id == "current" else None
                )
            ),
            get_current_chat_provider_id=AsyncMock(return_value="current"),
        )
        resolver = SessionReviewResolver(
            context,
            {"current_session_bot_enabled": True},
        )

        result = await resolver.resolve(
            SimpleNamespace(unified_msg_origin="test:group:room"),
            "missing-reviewer",
            agent_run_context=SimpleNamespace(messages=[]),
        )

        self.assertIs(result, current_provider)
        self.assertNotIsInstance(result, CurrentSessionReviewProvider)

    async def test_legacy_astrbot_uses_conversation_persona_before_default(
        self,
    ) -> None:
        provider = SimpleNamespace(
            text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="{}"))
        )
        conversation = SimpleNamespace(persona_id="alice", history="[]")
        persona_manager = SimpleNamespace(
            personas_v3=[
                {
                    "name": "alice",
                    "prompt": "旧版当前分支爱丽丝人格",
                    "_begin_dialogs_processed": [],
                }
            ],
            get_default_persona_v3=AsyncMock(
                return_value={"name": "default", "prompt": "全局默认人格"}
            ),
        )
        context = SimpleNamespace(
            conversation_manager=SimpleNamespace(
                get_curr_conversation_id=AsyncMock(return_value="cid"),
                get_conversation=AsyncMock(return_value=conversation),
            ),
            persona_manager=persona_manager,
        )
        reviewer = CurrentSessionReviewProvider(
            provider,
            context,
            SimpleNamespace(unified_msg_origin="test:group:room"),
            agent_run_context=None,
            context_turns=4,
        )

        await reviewer.text_chat(prompt="只返回 JSON", image_urls=["base64://image"])

        call = provider.text_chat.await_args.kwargs
        self.assertIn("旧版当前分支爱丽丝人格", call["system_prompt"])
        self.assertNotIn("全局默认人格", call["system_prompt"])
        persona_manager.get_default_persona_v3.assert_not_awaited()

    async def test_webchat_special_default_persona_is_inherited(self) -> None:
        provider = SimpleNamespace(
            text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="{}"))
        )
        context = SimpleNamespace(
            conversation_manager=SimpleNamespace(
                get_curr_conversation_id=AsyncMock(return_value="cid"),
                get_conversation=AsyncMock(
                    return_value=SimpleNamespace(persona_id="", history="[]")
                ),
            ),
            persona_manager=SimpleNamespace(
                resolve_selected_persona=AsyncMock(
                    return_value=("_chatui_default_", None, None, True)
                )
            ),
            get_config=Mock(return_value={"provider_settings": {}}),
        )
        reviewer = CurrentSessionReviewProvider(
            provider,
            context,
            SimpleNamespace(
                unified_msg_origin="webchat:private:user",
                get_platform_name=lambda: "webchat",
            ),
            agent_run_context=None,
            context_turns=4,
        )

        await reviewer.text_chat(prompt="只返回 JSON", image_urls=["base64://image"])

        self.assertIn(
            "calm, patient friend",
            provider.text_chat.await_args.kwargs["system_prompt"],
        )


if __name__ == "__main__":
    unittest.main()
