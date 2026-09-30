from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fanvpn_bridge.zen_models import (
    FAILURE_RETIREMENT_THRESHOLD,
    ZEN_SLUG_PREFIX,
    ZenModelCatalog,
    ZenModelError,
    _free_model_ids_from,
    build_catalog_entry,
    probe_model,
)
from fanvpn_bridge.zen_provider import (
    ZenProvider,
    ZenProviderError,
    _parse_chat_stream,
    _to_chat_request,
    is_zen_model,
)


_METADATA = {
    "cost": {"input": 0, "output": 0},
    "name": "Space Bunny Free",
    "reasoning": True,
    "reasoning_options": [{"type": "effort", "values": ["low", "medium", "high", "xhigh", "max"]}],
    "limit": {"context": 1_048_576},
    "modalities": {"input": ["text", "image"]},
    "tool_call": True,
}


def _catalog_document(models: dict[str, dict]) -> dict:
    return {"opencode": {"id": "opencode", "models": models}}


def _completion(content: str = "pong", reasoning: str = "thinking") -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "space-bunny-free",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content, "reasoning_content": reasoning},
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "completion_tokens_details": {"reasoning_tokens": 8},
            },
        }
    ).encode("utf-8")


class ZenCatalogDiscoveryTests(unittest.TestCase):
    def test_only_zero_cost_entries_are_catalog_free(self) -> None:
        document = _catalog_document(
            {
                "space-bunny-free": _METADATA,
                "paid-model": {**_METADATA, "cost": {"input": 3, "output": 15}},
                "no-cost-field": {"name": "Unknown"},
                "broken": "not-a-mapping",
            }
        )
        self.assertEqual(_free_model_ids_from(document), ["space-bunny-free"])

    def test_missing_provider_is_reported(self) -> None:
        with self.assertRaises(ZenModelError):
            _free_model_ids_from({"other": {}})
        with self.assertRaises(ZenModelError):
            _free_model_ids_from("not-a-mapping")

    def test_catalog_entry_uses_a_namespaced_slug_and_prefixed_display_name(self) -> None:
        entry = build_catalog_entry("space-bunny-free", _METADATA)
        self.assertEqual(entry["id"], ZEN_SLUG_PREFIX + "space-bunny-free")
        self.assertEqual(entry["display_name"], "Zen \u203a Space Bunny Free")
        self.assertEqual(entry["context_window"], 1_048_576)
        self.assertEqual(entry["auto_compact_token_limit"], 943_718)
        self.assertEqual(entry["input_modalities"], ["text", "image"])
        self.assertEqual(entry["supported_reasoning_levels"], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(entry["default_reasoning_level"], "medium")

    def test_catalog_entry_falls_back_when_metadata_is_sparse(self) -> None:
        entry = build_catalog_entry("mystery-free", {})
        self.assertEqual(entry["display_name"], "Zen \u203a mystery-free")
        self.assertEqual(entry["context_window"], 1_000_000)
        self.assertEqual(entry["input_modalities"], ["text"])
        # No reasoning metadata means no effort ladder is advertised at all.
        self.assertNotIn("supported_reasoning_levels", entry)

    def test_unknown_effort_values_are_dropped(self) -> None:
        entry = build_catalog_entry(
            "weird-free",
            {**_METADATA, "reasoning_options": [{"type": "effort", "values": ["low", "ultra", "high"]}]},
        )
        self.assertEqual(entry["supported_reasoning_levels"], ["low", "high"])

    def test_probe_requires_a_parsable_choice(self) -> None:
        with patch("fanvpn_bridge.zen_models.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = _completion()
            self.assertTrue(probe_model("space-bunny-free"))
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"choices":[]}'
            self.assertFalse(probe_model("space-bunny-free"))
            urlopen.return_value.__enter__.return_value.read.return_value = b"not json"
            self.assertFalse(probe_model("space-bunny-free"))


class ZenCatalogRefreshTests(unittest.TestCase):
    def test_refresh_publishes_only_models_that_answer(self) -> None:
        document = _catalog_document(
            {
                "reachable-free": _METADATA,
                "gated-free": _METADATA,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "zen-models.json"
            catalog = ZenModelCatalog(cache_path=cache)
            with patch("fanvpn_bridge.zen_models._fetch_json", side_effect=[document, {"data": [
                {"id": "reachable-free"},
                {"id": "gated-free"},
            ]}]), patch("fanvpn_bridge.zen_models._probe_all", return_value={
                "reachable-free": True,
                "gated-free": False,
            }):
                entries = catalog.refresh(force=True)
            self.assertEqual([entry["id"] for entry in entries], [ZEN_SLUG_PREFIX + "reachable-free"])
            self.assertTrue(cache.is_file())

    def test_models_missing_from_the_live_endpoint_are_never_published(self) -> None:
        document = _catalog_document({"withdrawn-free": _METADATA})
        with tempfile.TemporaryDirectory() as directory:
            catalog = ZenModelCatalog(cache_path=Path(directory) / "zen-models.json")
            with patch("fanvpn_bridge.zen_models._fetch_json", side_effect=[document, {"data": []}]), patch(
                "fanvpn_bridge.zen_models._probe_all", return_value={}
            ):
                self.assertEqual(catalog.refresh(force=True), [])

    def test_a_single_failed_probe_keeps_the_last_known_entry(self) -> None:
        document = _catalog_document({"flaky-free": _METADATA})
        with tempfile.TemporaryDirectory() as directory:
            catalog = ZenModelCatalog(cache_path=Path(directory) / "zen-models.json")
            with patch("fanvpn_bridge.zen_models._fetch_json", side_effect=[
                document,
                {"data": [{"id": "flaky-free"}]},
            ]), patch("fanvpn_bridge.zen_models._probe_all", return_value={"flaky-free": True}):
                catalog.refresh(force=True)

            failing = [{"data": [{"id": "flaky-free"}]}]
            for _ in range(FAILURE_RETIREMENT_THRESHOLD - 1):
                with patch("fanvpn_bridge.zen_models._fetch_json", side_effect=[document, *failing]), patch(
                    "fanvpn_bridge.zen_models._probe_all", return_value={"flaky-free": False}
                ):
                    self.assertEqual(len(catalog.refresh(force=True)), 1)

            with patch("fanvpn_bridge.zen_models._fetch_json", side_effect=[document, *failing]), patch(
                "fanvpn_bridge.zen_models._probe_all", return_value={"flaky-free": False}
            ):
                self.assertEqual(catalog.refresh(force=True), [])

    def test_entries_survive_a_reload(self) -> None:
        document = _catalog_document({"cached-free": _METADATA})
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "zen-models.json"
            with patch("fanvpn_bridge.zen_models._fetch_json", side_effect=[
                document,
                {"data": [{"id": "cached-free"}]},
            ]), patch("fanvpn_bridge.zen_models._probe_all", return_value={"cached-free": True}):
                ZenModelCatalog(cache_path=cache).refresh(force=True)
            reloaded = ZenModelCatalog(cache_path=cache)
            self.assertEqual(
                [entry["id"] for entry in reloaded.entries()],
                [ZEN_SLUG_PREFIX + "cached-free"],
            )

    def test_unverified_slug_still_resolves_for_a_useful_error(self) -> None:
        catalog = ZenModelCatalog()
        self.assertEqual(catalog.resolve_model_id("zen/not-probed"), "not-probed")
        self.assertIsNone(catalog.resolve_model_id("deepseek-web/chat"))
        self.assertIsNone(catalog.resolve_model_id("zen/"))


class ZenRequestTranslationTests(unittest.TestCase):
    def test_zen_prefix_does_not_capture_other_providers(self) -> None:
        self.assertTrue(is_zen_model("zen/space-bunny-free"))
        for value in ("deepseek-web/chat", "chatgpt-web/high", "gemini-3.7-flash-tiered", "zenx/model", 7):
            self.assertFalse(is_zen_model(value))

    def test_input_text_and_output_text_both_lower_to_chat_text(self) -> None:
        body = _to_chat_request(
            {
                "instructions": "Be brief.",
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
                ],
            },
            "space-bunny-free",
            stream=False,
        )
        self.assertEqual(
            body["messages"],
            [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hello"}],
        )
        self.assertNotIn("stream", body)

    def test_tool_history_lowers_to_assistant_and_tool_messages(self) -> None:
        body = _to_chat_request(
            {
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "compute"}]},
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "calc",
                        "arguments": '{"expression":"17*23"}',
                    },
                    {"type": "function_call_output", "call_id": "call_1", "output": "391"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "calc",
                        "description": "Evaluate arithmetic",
                        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}},
                    }
                ],
            },
            "space-bunny-free",
            stream=False,
        )
        roles = [message["role"] for message in body["messages"]]
        self.assertEqual(roles, ["user", "assistant", "tool"])
        self.assertEqual(body["messages"][1]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(body["messages"][2]["tool_call_id"], "call_1")
        self.assertEqual(body["tools"][0]["function"]["name"], "calc")

    def test_custom_tool_becomes_a_single_string_function(self) -> None:
        body = _to_chat_request(
            {
                "input": [
                    {
                        "type": "custom_tool_call",
                        "call_id": "call_2",
                        "name": "apply_patch",
                        "input": "raw diff",
                    },
                    {"type": "custom_tool_call_output", "call_id": "call_2", "output": "applied"},
                ],
                "tools": [{"type": "custom", "name": "apply_patch", "description": "Apply a patch"}],
            },
            "space-bunny-free",
            stream=False,
        )
        function = body["tools"][0]["function"]
        self.assertEqual(function["parameters"]["required"], ["input"])
        self.assertEqual([message["role"] for message in body["messages"]], ["assistant", "tool"])
        self.assertEqual(body["messages"][1]["tool_call_id"], "call_2")
        self.assertEqual(body["messages"][1]["content"], "applied")
        self.assertEqual(json.loads(body["messages"][0]["tool_calls"][0]["function"]["arguments"]), {"input": "raw diff"})

    def test_parallel_mixed_tool_history_is_one_batch_even_without_current_tools(self) -> None:
        body = _to_chat_request(
            {"input": [
                {"type": "message", "role": "user", "content": "check"},
                {"type": "function_call", "call_id": "f1", "name": "calc", "arguments": "{}"},
                {"type": "reasoning", "summary": []},
                {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "noop"},
                {"type": "function_call_output", "call_id": "f1", "output": "42"},
                {"type": "custom_tool_call_output", "call_id": "c1", "output": "OK"},
                {"type": "message", "role": "assistant", "content": "done"},
                {"type": "message", "role": "user", "content": "continue"},
                {"type": "custom_tool_call", "call_id": "c2", "name": "exec", "input": "next"},
                {"type": "custom_tool_call_output", "call_id": "c2", "output": "OK"},
            ]},
            "space-bunny-free",
            stream=True,
        )
        messages = body["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool", "tool", "assistant", "user", "assistant", "tool"])
        self.assertEqual([call["id"] for call in messages[1]["tool_calls"]], ["f1", "c1"])
        self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"], ["f1", "c1", "c2"])
        self.assertEqual([call["id"] for call in messages[6]["tool_calls"]], ["c2"])

    def test_reasoning_effort_and_stream_flag_are_forwarded(self) -> None:
        body = _to_chat_request(
            {
                "reasoning": {"effort": "XHIGH"},
                "max_output_tokens": 512,
                "input": [{"type": "message", "role": "user", "content": "hi"}],
            },
            "space-bunny-free",
            stream=True,
        )
        self.assertEqual(body["reasoning_effort"], "xhigh")
        self.assertEqual(body["max_tokens"], 512)
        self.assertIs(body["stream"], True)

    def test_reasoning_items_are_not_echoed_back_as_messages(self) -> None:
        body = _to_chat_request(
            {
                "input": [
                    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "prior"}]},
                    {"type": "message", "role": "user", "content": "next"},
                ]
            },
            "space-bunny-free",
            stream=False,
        )
        self.assertEqual(body["messages"], [{"role": "user", "content": "next"}])


class ZenStreamParsingTests(unittest.TestCase):
    def test_sse_blocks_and_done_sentinel_are_handled(self) -> None:
        raw = (
            b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            b"event: custom\ndata: {\"choices\":[{\"delta\":{\"content\":\"b\"}}]}\n\n"
            b"data: [DONE]\n\n"
        )
        chunks = list(_parse_chat_stream(raw))
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "a")
        self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], "b")

    def test_malformed_chunk_is_skipped(self) -> None:
        raw = b"data: {oops\n\ndata: {\"choices\":[]}\n\n"
        self.assertEqual(list(_parse_chat_stream(raw)), [{"choices": []}])


class ZenProviderTests(unittest.TestCase):
    def test_ordinary_model_list_refreshes_stale_cache_and_discovers_new_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "zen-models.json"
            catalog = ZenModelCatalog(cache_path=cache)
            with patch("fanvpn_bridge.zen_models._fetch_json", side_effect=[
                _catalog_document({"known": _METADATA}), {"data": [{"id": "known"}]},
            ]), patch("fanvpn_bridge.zen_models._probe_all", return_value={"known": True}):
                catalog.refresh(force=True)
            provider = ZenProvider(catalog=catalog)
            with patch("fanvpn_bridge.zen_models._fetch_json") as fetch:
                self.assertEqual([row["id"] for row in provider.models_response()["data"]], ["zen/known"])
                fetch.assert_not_called()
            os.utime(cache, (0, 0))
            with patch("fanvpn_bridge.zen_models._fetch_json", side_effect=[
                _catalog_document({"known": _METADATA, "new": _METADATA}),
                {"data": [{"id": "known"}, {"id": "new"}]},
            ]), patch("fanvpn_bridge.zen_models._probe_all", return_value={"known": True, "new": True}):
                self.assertEqual({row["id"] for row in provider.models_response()["data"]}, {"zen/known", "zen/new"})
            self.assertEqual({row["id"] for row in ZenModelCatalog(cache_path=cache).entries()}, {"zen/known", "zen/new"})

    def test_ordinary_stale_refresh_keeps_cached_models_when_catalog_is_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "zen-models.json"
            catalog = ZenModelCatalog(cache_path=cache)
            catalog._entries["zen/known"] = {"id": "zen/known"}
            catalog._save()
            os.utime(cache, (0, 0))
            with patch("fanvpn_bridge.zen_models._fetch_json", side_effect=ZenModelError("offline")) as fetch:
                self.assertEqual(ZenProvider(catalog=catalog).models_response()["data"], [{"id": "zen/known"}])
                fetch.assert_called_once()

    def test_model_list_survives_an_unreachable_catalog(self) -> None:
        provider = ZenProvider(catalog=ZenModelCatalog())
        with patch("fanvpn_bridge.zen_models._fetch_json", side_effect=ZenModelError("offline")):
            self.assertEqual(provider.models_response()["data"], [])

    def test_a_failing_refresh_keeps_the_last_verified_models(self) -> None:
        catalog = ZenModelCatalog()
        catalog._entries[ZEN_SLUG_PREFIX + "known"] = {"id": ZEN_SLUG_PREFIX + "known"}
        provider = ZenProvider(catalog=catalog)
        with patch("fanvpn_bridge.zen_models._fetch_json", side_effect=ZenModelError("offline")):
            data = provider.models_response(force=True)["data"]
        self.assertEqual([entry["id"] for entry in data], [ZEN_SLUG_PREFIX + "known"])

    def test_unsupported_model_is_rejected_before_any_request(self) -> None:
        provider = ZenProvider()
        with self.assertRaises(ZenProviderError) as caught:
            provider.responses({"model": "deepseek-web/chat", "input": []})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "zen_model_unsupported")

    def test_text_answer_becomes_a_completed_response(self) -> None:
        provider = ZenProvider()
        with patch.object(ZenProvider, "_post", return_value=_completion("pong")):
            streaming, result = provider.responses(
                {"model": "zen/space-bunny-free", "input": [{"type": "message", "role": "user", "content": "hi"}]}
            )
        self.assertFalse(streaming)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["model"], "zen/space-bunny-free")
        self.assertEqual(result["output"][0]["content"][0]["text"], "pong")
        self.assertEqual(result["usage"], {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})

    def test_tool_call_becomes_a_function_call_item(self) -> None:
        raw = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_function_1",
                                    "type": "function",
                                    "function": {"name": "calc", "arguments": '{"expression":"17*23"}'},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        ).encode("utf-8")
        provider = ZenProvider()
        with patch.object(ZenProvider, "_post", return_value=raw):
            _streaming, result = provider.responses(
                {
                    "model": "zen/space-bunny-free",
                    "input": [{"type": "message", "role": "user", "content": "compute"}],
                    "tools": [{"type": "function", "name": "calc", "parameters": {"type": "object"}}],
                }
            )
        call = next(item for item in result["output"] if item["type"] == "function_call")
        self.assertEqual(call["name"], "calc")
        self.assertEqual(call["call_id"], "call_function_1")
        self.assertEqual(json.loads(call["arguments"]), {"expression": "17*23"})

    def test_streaming_request_emits_the_full_event_sequence(self) -> None:
        raw = (
            b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"pong"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n'
            b"data: [DONE]\n\n"
        )
        provider = ZenProvider()
        with patch.object(ZenProvider, "_post", return_value=raw):
            streaming, result = provider.responses(
                {
                    "model": "zen/space-bunny-free",
                    "stream": True,
                    "input": [{"type": "message", "role": "user", "content": "hi"}],
                }
            )
        self.assertTrue(streaming)
        events = list(result)
        names = [
            line[7:]
            for raw_event in events
            for line in raw_event.decode("utf-8").splitlines()
            if line.startswith("event: ")
        ]
        self.assertEqual(
            names,
            [
                "response.created",
                "response.output_item.added",
                "response.content_part.added",
                "response.output_text.delta",
                "response.output_text.done",
                "response.content_part.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual(events[-1], b"data: [DONE]\n\n")

    def test_custom_tool_roundtrip_preserves_raw_input_across_multiple_turns(self) -> None:
        raw_input = '*** Begin Patch\n*** Add File: note.md\n+# 中文 "引号" \\ 路径\n+```python\n+print("你好")\n+```\n*** End Patch'
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                payload = {
                    "model": "zen/space-bunny-free", "stream": streaming,
                    "input": [{"type": "message", "role": "user", "content": "test"}],
                    "tools": [{"type": "custom", "name": "apply_patch"}],
                }
                provider = ZenProvider()
                for turn in range(3):
                    call_id = f"call_{turn}"
                    arguments = json.dumps({"input": raw_input}, ensure_ascii=False)
                    chunks = [
                        {"choices": [{"delta": {"tool_calls": [{
                            "index": 0, "id": call_id,
                            "function": {"name": "apply_patch", "arguments": arguments[:17]},
                        }]}}]},
                        {"choices": [{"delta": {"tool_calls": [{
                            "index": 0, "function": {"arguments": arguments[17:]},
                        }]}}]},
                    ]
                    if streaming:
                        raw = b"".join(b"data: " + json.dumps(c, ensure_ascii=False).encode("utf-8") + b"\n\n" for c in chunks)
                        raw += b"data: [DONE]\n\n"
                    else:
                        raw = json.dumps({"choices": [{"message": {"tool_calls": [{
                            "id": call_id, "function": {"name": "apply_patch", "arguments": arguments},
                        }]}}]}).encode("utf-8")
                    with patch.object(ZenProvider, "_post", return_value=raw) as post:
                        _, result = provider.responses(payload)
                    sent = post.call_args[0][0]["messages"]
                    self.assertEqual(len([m for m in sent if m["role"] == "tool"]), turn)
                    for index, message in enumerate(sent):
                        if message["role"] == "tool":
                            self.assertEqual(message["tool_call_id"], sent[index - 1]["tool_calls"][0]["id"])
                    if streaming:
                        events = [json.loads(line[6:]) for event in result for line in event.decode("utf-8").splitlines() if line.startswith("data: {")]
                        delta = next(e for e in events if e["type"] == "response.custom_tool_call_input.delta")
                        done = next(e for e in events if e["type"] == "response.custom_tool_call_input.done")
                        self.assertEqual(delta["delta"], raw_input)
                        self.assertEqual(done["input"], raw_input)
                        self.assertFalse(any(e["type"].startswith("response.function_call_arguments") for e in events))
                        result = events[-1]["response"]
                    call = result["output"][0]
                    self.assertEqual(call["type"], "custom_tool_call")
                    self.assertEqual(call["call_id"], call_id)
                    self.assertEqual(call["input"], raw_input)
                    self.assertNotIn("arguments", call)
                    payload["input"].extend([call, {"type": "custom_tool_call_output", "call_id": call_id, "output": "applied"}])

    def test_invalid_custom_tool_arguments_are_rejected_without_repair(self) -> None:
        for streaming in (False, True):
            for arguments in ('{broken', '{}', '{"input":42}', '[]'):
                with self.subTest(streaming=streaming, arguments=arguments):
                    call = {"index": 0, "id": "bad", "function": {"name": "exec", "arguments": arguments}}
                    if streaming:
                        raw = b"data: " + json.dumps({"choices": [{"delta": {"tool_calls": [call]}}]}).encode() + b"\n\ndata: [DONE]\n\n"
                    else:
                        raw = json.dumps({"choices": [{"message": {"tool_calls": [call]}}]}).encode()
                    with patch.object(ZenProvider, "_post", return_value=raw):
                        with self.assertRaises(ZenProviderError) as caught:
                            ZenProvider().responses({"model": "zen/space-bunny-free", "stream": streaming, "tools": [{"type": "custom", "name": "exec"}]})
                    self.assertEqual(caught.exception.code, "zen_custom_tool_invalid")

    def test_compaction_summarizes_history_through_the_model(self) -> None:
        provider = ZenProvider()
        payload = {
            "model": "zen/space-bunny-free",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "long history"}]},
                {"type": "function_call", "call_id": "c1", "name": "calc", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "391"},
            ],
            "client_metadata": {"x-codex-turn-metadata": json.dumps({"request_kind": "compaction"})},
        }
        with patch.object(ZenProvider, "_post", return_value=_completion("SUMMARY")) as post:
            result = provider.compact(payload)
        sent = post.call_args[0][0]["messages"][1]["content"]
        self.assertIn("USER: long history", sent)
        self.assertIn("TOOL CALL: calc", sent)
        self.assertEqual(result["output"][0]["content"][0]["text"], "SUMMARY")

    def test_upstream_http_error_is_wrapped(self) -> None:
        import urllib.error
        import io

        provider = ZenProvider()
        error = urllib.error.HTTPError(
            "https://opencode.ai/zen/v1/chat/completions", 400, "Bad Request", {}, io.BytesIO(b'{"error":"nope"}')
        )
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                with patch("fanvpn_bridge.zen_provider.urllib.request.urlopen", side_effect=error):
                    with self.assertRaises(ZenProviderError) as caught:
                        provider.responses(
                            {"model": "zen/space-bunny-free", "stream": streaming, "input": [{"type": "message", "role": "user", "content": "hi"}]}
                        )
                self.assertEqual(caught.exception.status, 400)
                self.assertEqual(caught.exception.code, "zen_upstream_failed")


if __name__ == "__main__":
    unittest.main()
