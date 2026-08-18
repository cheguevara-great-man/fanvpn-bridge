from __future__ import annotations

import json
import unittest

from fanvpn_bridge.gemini_account import (
    ThoughtSignatureCache,
    _gemini_to_responses_events,
    _normalize_available_models,
    _responses_to_gemini,
)


class GeminiAccountTranslationTests(unittest.TestCase):
    def test_internal_aliases_become_one_model_family_with_real_effort_routes(self) -> None:
        choices = _normalize_available_models(
            {
                "gemini-3.7-flash-tiered": {"thinkingBudget": -1},
                "gemini-3.6-flash-tiered": {"thinkingBudget": -1},
                "gemini-3.6-flash-high": {"displayName": "Gemini 3.6 Flash (High)"},
                "gemini-3.5-flash-extra-low": {"displayName": "Gemini 3.5 Flash (Low)"},
                "gemini-3.5-flash-low": {"displayName": "Gemini 3.5 Flash (Medium)"},
                "gemini-3-flash-agent": {"displayName": "Gemini 3.5 Flash (High)"},
                "gemini-3.1-pro-low": {"displayName": "Gemini 3.1 Pro (Low)"},
                "gemini-3.1-pro-high": {"displayName": "Gemini 3.1 Pro (High)"},
                "gemini-pro-agent": {"displayName": "Gemini 3.1 Pro (High)"},
                "gemini-2.5-pro": {"displayName": "Gemini 2.5 Pro"},
            }
        )
        by_id = {choice.id: choice for choice in choices}

        self.assertEqual(
            list(by_id),
            ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-pro"],
        )
        self.assertEqual(by_id["gemini-3.7-flash"].supported_reasoning_levels, ("low", "medium", "high"))
        self.assertEqual(set(dict(by_id["gemini-3.6-flash"].routes).values()), {"gemini-3.6-flash-tiered"})
        self.assertEqual(
            dict(by_id["gemini-3.5-flash"].routes),
            {
                "low": "gemini-3.5-flash-extra-low",
                "medium": "gemini-3.5-flash-low",
                "high": "gemini-3-flash-agent",
            },
        )
        self.assertEqual(by_id["gemini-3.1-pro"].supported_reasoning_levels, ("low", "high"))
        self.assertEqual(
            dict(by_id["gemini-3.1-pro"].routes),
            {"low": "gemini-3.1-pro-low", "high": "gemini-pro-agent"},
        )

    def test_signature_cache_is_bounded_and_keeps_recent_calls(self) -> None:
        signatures = ThoughtSignatureCache(limit=2)
        signatures["call_1"] = "one"
        signatures["call_2"] = "two"
        self.assertEqual(signatures.get("call_1"), "one")
        signatures["call_3"] = "three"

        self.assertNotIn("call_2", signatures)
        self.assertEqual(signatures.get("call_1"), "one")
        self.assertEqual(signatures.get("call_3"), "three")

    def test_codex_remains_agent_and_tools_become_gemini_declarations(self) -> None:
        request = _responses_to_gemini(
            {
                "model": "gemini-3.6-flash-high",
                "instructions": "You are Codex. Use the supplied tools.",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Read README.md"}],
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "read_file",
                        "description": "Read a workspace file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    }
                ],
                "reasoning": {"effort": "high"},
            },
            {},
        )

        self.assertEqual(request["contents"][0]["role"], "user")
        self.assertEqual(request["contents"][0]["parts"][0]["text"], "Read README.md")
        self.assertIn("You are Codex", request["systemInstruction"]["parts"][0]["text"])
        declaration = request["tools"][0]["functionDeclarations"][0]
        self.assertEqual(declaration["name"], "read_file")
        self.assertEqual(request["toolConfig"]["functionCallingConfig"]["mode"], "VALIDATED")
        self.assertEqual(request["generationConfig"]["thinkingConfig"]["thinkingLevel"], "high")

    def test_tool_result_round_trip_preserves_call_identity_and_signature(self) -> None:
        request = _responses_to_gemini(
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_read_1",
                        "name": "read_file",
                        "arguments": '{"path":"README.md"}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_read_1",
                        "output": "contents",
                    },
                ]
            },
            {"call_read_1": "provider-signature"},
        )

        call = request["contents"][0]["parts"][0]
        result = request["contents"][1]["parts"][0]
        self.assertEqual(call["functionCall"]["id"], "call_read_1")
        self.assertEqual(call["thoughtSignature"], "provider-signature")
        self.assertEqual(result["functionResponse"]["id"], "call_read_1")
        self.assertEqual(result["functionResponse"]["name"], "read_file")

    def test_gemini_stream_becomes_responses_text_and_function_events(self) -> None:
        signatures: dict[str, str] = {}
        chunks = [
            {
                "response": {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "I will inspect it."},
                                    {
                                        "functionCall": {
                                            "id": "call_1",
                                            "name": "read_file",
                                            "args": {"path": "README.md"},
                                        },
                                        "thoughtSignature": "native-signature",
                                    },
                                ]
                            }
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 10,
                        "candidatesTokenCount": 5,
                        "totalTokenCount": 15,
                    },
                }
            }
        ]
        raw_events = list(_gemini_to_responses_events(chunks, "gemini-test", signatures))
        payloads = []
        for raw in raw_events:
            for line in raw.decode().splitlines():
                if line.startswith("data: {"):
                    payloads.append(json.loads(line[6:]))
        types = [payload["type"] for payload in payloads]
        self.assertIn("response.output_text.delta", types)
        self.assertIn("response.function_call_arguments.done", types)
        self.assertEqual(types[-1], "response.completed")
        self.assertEqual(signatures["call_1"], "native-signature")
        completed = payloads[-1]["response"]
        self.assertEqual(completed["usage"]["total_tokens"], 15)
        self.assertEqual([item["type"] for item in completed["output"]], ["message", "function_call"])

    def test_output_order_is_preserved_when_function_call_precedes_text(self) -> None:
        chunks = [
            {
                "response": {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"functionCall": {"id": "call_1", "name": "inspect", "args": {}}},
                                    {"text": "After the call."},
                                ]
                            }
                        }
                    ]
                }
            }
        ]
        payloads = []
        for raw in _gemini_to_responses_events(chunks, "gemini-test", {}):
            for line in raw.decode().splitlines():
                if line.startswith("data: {"):
                    payloads.append(json.loads(line[6:]))

        completed = payloads[-1]["response"]
        self.assertEqual([item["type"] for item in completed["output"]], ["function_call", "message"])
        text_events = [item for item in payloads if item["type"] == "response.output_text.delta"]
        self.assertEqual(text_events[0]["output_index"], 1)


    def test_cached_tokens_reported_in_usage(self) -> None:
        chunks = [
            {
                "response": {
                    "usageMetadata": {
                        "promptTokenCount": 1000,
                        "cachedContentTokenCount": 800,
                        "candidatesTokenCount": 50,
                        "totalTokenCount": 1050,
                    },
                    "candidates": [
                        {"content": {"parts": [{"text": "Hello"}]}}
                    ],
                }
            }
        ]
        payloads = []
        for raw in _gemini_to_responses_events(chunks, "gemini-3.7-flash", {}):
            for line in raw.decode().splitlines():
                if line.startswith("data: {"):
                    payloads.append(json.loads(line[6:]))

        completed = payloads[-1]["response"]
        usage = completed["usage"]
        self.assertEqual(usage["input_tokens"], 1000)
        self.assertEqual(usage["input_tokens_details"]["cached_tokens"], 800)
        self.assertEqual(usage["output_tokens"], 50)
        self.assertEqual(usage["total_tokens"], 1050)

    def test_oversized_images_are_downscaled(self) -> None:
        import base64
        import io
        from PIL import Image

        img = Image.new("RGB", (3200, 2000), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        data_url = f"data:image/png;base64,{b64}"

        payload = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Inspect image"},
                        {"type": "input_image", "image_url": data_url},
                    ],
                }
            ]
        }
        gemini_req = _responses_to_gemini(payload, {})
        contents = gemini_req["contents"]
        parts = contents[0]["parts"]
        image_part = next(p for p in parts if "inlineData" in p)
        resized_bytes = base64.b64decode(image_part["inlineData"]["data"])
        with Image.open(io.BytesIO(resized_bytes)) as resized_img:
            self.assertLessEqual(max(resized_img.size), 1536)


    def test_gemini_thought_stream_converted_to_responses_reasoning(self) -> None:
        chunks = [
            {
                "response": {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "thought": True,
                                        "text": "**Planning investigation**\nChecking the files."
                                    }
                                ]
                            }
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 60,
                        "thoughtsTokenCount": 25,
                        "candidatesTokenCount": 15,
                        "totalTokenCount": 100
                    }
                }
            },
            {
                "response": {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": "Done."
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        ]
        events = []
        for raw in _gemini_to_responses_events(chunks, "gemini-3.7-flash", {}):
            for line in raw.decode().splitlines():
                if line.startswith("data: {"):
                    events.append(json.loads(line[6:]))

        event_types = [e["type"] for e in events]
        self.assertIn("response.reasoning_summary_part.added", event_types)
        self.assertIn("response.reasoning_summary_text.delta", event_types)
        self.assertIn("response.reasoning_summary_text.done", event_types)

        completed = events[-1]["response"]
        output_types = [item["type"] for item in completed["output"]]
        self.assertEqual(output_types, ["reasoning", "message"])
        self.assertEqual(
            completed["output"][0]["summary"][0]["text"],
            "**Planning investigation**\nChecking the files."
        )
        self.assertEqual(completed["output"][1]["content"][0]["text"], "Done.")


if __name__ == "__main__":

    unittest.main()
