from __future__ import annotations

import base64
import json
import unittest

from fanvpn_bridge.deepseek_harness import (
    DeepSeekHarnessProvider,
    _deepseek_response_message_id,
    _encode_pow_response,
    _parse_deepseek_stream,
    _parse_tool_calls,
    _tool_call_protocol_error,
    _responses_events,
    _responses_to_deepseek_prompt,
)


class DeepSeekHarnessTests(unittest.TestCase):
    def test_models_are_exposed_under_a_separate_prefix(self) -> None:
        provider = DeepSeekHarnessProvider(pow_solver=lambda _challenge: 0)
        ids = [item["id"] for item in provider.models_response()["data"]]
        self.assertEqual(ids, ["deepseek-web/chat", "deepseek-web/reasoner"])

    def test_responses_prompt_keeps_codex_as_tool_executor(self) -> None:
        prompt = _responses_to_deepseek_prompt(
            {
                "instructions": "Follow the repository rules.",
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "inspect it"}]},
                    {"type": "function_call", "call_id": "call_old", "name": "read_file", "arguments": "{\"path\":\"a.py\"}"},
                    {"type": "function_call_output", "call_id": "call_old", "output": "contents"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "read_file",
                        "description": "Read one file",
                        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    },
                    {
                        "type": "function",
                        "name": "exec_command",
                        "description": "Run a command",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "cmd": {"type": "string"},
                                "workdir": {"type": "string"},
                                "max_output_tokens": {"type": "integer"},
                            },
                            "required": ["cmd"],
                        },
                    },
                ],
            }
        )
        self.assertIn("Follow the repository rules.", prompt)
        self.assertIn("USER:\ninspect it", prompt)
        self.assertIn("TOOL RESULT (call_old):\ncontents", prompt)
        self.assertIn("Codex, not you, executes tools", prompt)
        self.assertIn("### Tool read_file", prompt)
        self.assertIn("### Tool exec_command", prompt)
        self.assertIn("<read_file>\n{\"path\":\"path/to/file\"}\n</read_file>", prompt)
        self.assertIn("<exec_command>\n", prompt)
        self.assertIn('"cmd":"Get-Content a.txt"', prompt)
        self.assertIn("Parameters JSON Schema", prompt)
        self.assertIn("Never use `<codex_tool_call>`", prompt)
        self.assertIn("Do not wrap arguments in `name`, `arguments`, or `tool`", prompt)

    def test_deepseek_stream_separates_thinking_from_answer(self) -> None:
        raw = (
            'data: {"p":"response/fragments","o":"APPEND","v":'
            '[{"type":"THINK","content":"private"},{"type":"RESPONSE","content":"hello"}]}\n\n'
            'data: {"p":"response/fragments/-1/content","v":" world"}\n\n'
            'data: {"p":"response/status","v":"FINISHED"}\n\n'
        ).encode()
        text, reasoning = _parse_deepseek_stream(raw)
        self.assertEqual(text, "hello world")
        self.assertEqual(reasoning, "private")

    def test_deepseek_stream_exposes_parent_message_id_for_continuation(self) -> None:
        raw = (
            'data: {"p":"response/message_id","v":"message-123"}\n\n'
            'data: {"p":"response/status","v":"FINISHED"}\n\n'
        ).encode()
        self.assertEqual(_deepseek_response_message_id(raw), "message-123")

    def test_valid_tool_blocks_become_function_calls_even_with_surrounding_prose(self) -> None:
        text = '<codex_tool_call>{"name":"read_file","arguments":{"path":"a.py"}}</codex_tool_call>'
        calls = _parse_tool_calls(text, {"read_file"})
        self.assertEqual(calls, [{"name": "read_file", "arguments": {"path": "a.py"}}])
        self.assertEqual(
            _parse_tool_calls("I will inspect it first.\n\n" + text, {"read_file"}),
            [{"name": "read_file", "arguments": {"path": "a.py"}}],
        )
        self.assertIsNone(_parse_tool_calls(text, {"different_tool"}))

    def test_direct_per_tool_tags_become_function_calls(self) -> None:
        text = (
            '<exec_command>{"cmd":"Get-Content a.txt","max_output_tokens":3000}</exec_command>\n'
            '<write_stdin>{"session_id":12,"chars":"y\\n"}</write_stdin>'
        )
        self.assertEqual(
            _parse_tool_calls(text, {"exec_command", "write_stdin"}),
            [
                {"name": "exec_command", "arguments": {"cmd": "Get-Content a.txt", "max_output_tokens": 3000}},
                {"name": "write_stdin", "arguments": {"session_id": 12, "chars": "y\n"}},
            ],
        )

    def test_direct_tool_tag_requires_valid_json_object(self) -> None:
        self.assertIsNone(_parse_tool_calls('<exec_command>{"cmd":BROKEN}</exec_command>', {"exec_command"}))
        self.assertIsNone(_parse_tool_calls('<exec_command>["not-an-object"]</exec_command>', {"exec_command"}))
        self.assertIn(
            "invalid JSON arguments",
            _tool_call_protocol_error('<exec_command>{"cmd":"say "hello""}</exec_command>', {"exec_command"}) or "",
        )
        self.assertIn(
            "one JSON object",
            _tool_call_protocol_error('<exec_command>["not-an-object"]</exec_command>', {"exec_command"}) or "",
        )
        self.assertIn(
            "invalid JSON arguments",
            _tool_call_protocol_error(
                '<exec_command>{"cmd":"ok"}</exec_command>\n'
                '<exec_command>{"cmd":"say "broken""}</exec_command>',
                {"exec_command"},
            ) or "",
        )

    def test_legacy_codex_tool_call_remains_backward_compatible(self) -> None:
        text = '<codex_tool_call>{"name":"read_file","arguments":{"path":"a.py"}}</codex_tool_call>'
        self.assertEqual(
            _parse_tool_calls(text, {"read_file"}),
            [{"name": "read_file", "arguments": {"path": "a.py"}}],
        )

    def test_tool_blocks_repair_missing_outer_closing_brace(self) -> None:
        text = (
            'I will inspect it first.\n\n'
            '<codex_tool_call>{"name":"exec_command","arguments":'
            '{"cmd":"$code = @\'\\nprint(f\\\"x={value}\\\")\\n\'@\\n$code | python -",'
            '"workdir":"D:\\\\software\\\\Note","max_output_tokens":8000}</codex_tool_call>\n'
            '<codex_tool_call>{"name":"exec_command","arguments":'
            '{"cmd":"Get-Content a.txt","max_output_tokens":15000}</codex_tool_call>'
        )
        calls = _parse_tool_calls(text, {"exec_command"})
        self.assertIsNotNone(calls)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["arguments"]["workdir"], "D:\\software\\Note")
        self.assertEqual(calls[0]["arguments"]["max_output_tokens"], 8000)
        self.assertEqual(calls[1]["arguments"]["cmd"], "Get-Content a.txt")

    def test_tool_block_does_not_repair_malformed_json_in_middle(self) -> None:
        text = (
            '<codex_tool_call>{"name":"exec_command","arguments":'
            '{"cmd":BROKEN,"max_output_tokens":8000}</codex_tool_call>'
        )
        self.assertIsNone(_parse_tool_calls(text, {"exec_command"}))

    def test_flat_exec_command_arguments_are_normalized(self) -> None:
        text = (
            '<codex_tool_call>{"cmd":"Get-Content a.txt","workdir":"C:\\\\tmp",'
            '"max_output_tokens":4000}</codex_tool_call>'
        )
        self.assertEqual(
            _parse_tool_calls(text, {"exec_command"}),
            [
                {
                    "name": "exec_command",
                    "arguments": {
                        "cmd": "Get-Content a.txt",
                        "workdir": "C:\\tmp",
                        "max_output_tokens": 4000,
                    },
                }
            ],
        )
        self.assertIsNone(_parse_tool_calls(text, {"read_file"}))

    def test_dsml_exec_command_is_normalized_instead_of_leaking_as_text(self) -> None:
        text = (
            '<锝滐綔DSML锝滐綔 calls>\n'
            '<锝滐綔DSML锝滐綔 invoke name="exec_command">\n'
            '<锝滐綔DSML锝滐綔 parameter name="cmd" string="true">Get-Content a.txt</锝滐綔DSML锝滐綔 parameter>\n'
            '<锝滐綔DSML锝滐綔 parameter name="workdir" string="true">C:\\tmp</锝滐綔DSML锝滐綔 parameter>\n'
            '<锝滐綔DSML锝滐綔 parameter name="max_output_tokens" string="false">3000</锝滐綔DSML锝滐綔 parameter>\n'
            '</锝滐綔DSML锝滐綔 invoke>\n'
            '</锝滐綔DSML锝滐綔 calls>'
        )
        self.assertEqual(
            _parse_tool_calls(text, {"exec_command"}),
            [
                {
                    "name": "exec_command",
                    "arguments": {
                        "cmd": "Get-Content a.txt",
                        "workdir": "C:\\tmp",
                        "max_output_tokens": 3000,
                    },
                }
            ],
        )
        self.assertIsNone(_parse_tool_calls(text, {"read_file"}))

    def test_ascii_dsml_tool_call_is_also_normalized(self) -> None:
        text = (
            '<|DSML| invoke name="read_file">'
            '<|DSML| parameter name="path" string="true">a&amp;b.py</|DSML| parameter>'
            '</|DSML| invoke>'
        )
        self.assertEqual(
            _parse_tool_calls(text, {"read_file"}),
            [{"name": "read_file", "arguments": {"path": "a&b.py"}}],
        )

    def test_pow_response_matches_deepseek_web_shape(self) -> None:
        encoded = _encode_pow_response(
            {
                "algorithm": "DeepSeekHashV1",
                "challenge": "a" * 64,
                "salt": "salt",
                "signature": "signature",
                "expireAt": 123,
            },
            42,
        )
        value = json.loads(base64.b64decode(encoded))
        self.assertEqual(value["answer"], 42)
        self.assertEqual(value["target_path"], "/api/v0/chat/completion")

    def test_tool_call_is_returned_as_responses_function_call(self) -> None:
        events = list(
            _responses_events(
                "deepseek-web/chat",
                "",
                [{"name": "read_file", "arguments": {"path": "a.py"}}],
            )
        )
        decoded = [line[6:] for event in events for line in event.decode().splitlines() if line.startswith("data: {")]
        completed = next(json.loads(line) for line in decoded if json.loads(line).get("type") == "response.completed")
        output = completed["response"]["output"]
        self.assertEqual(output[0]["type"], "function_call")
        self.assertEqual(output[0]["name"], "read_file")
        self.assertEqual(json.loads(output[0]["arguments"]), {"path": "a.py"})

    def test_provider_runs_session_pow_completion_and_returns_tool_call(self) -> None:
        solved = []

        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda challenge: solved.append(challenge) or 23)
                self.requests = []

            def _request(self, method, upstream_path, body, headers):
                self.requests.append((method, upstream_path, body, dict(headers)))
                if upstream_path.endswith("chat_session/create"):
                    return 200, {}, json.dumps({
                        "data": {"biz_code": 0, "biz_data": {"chat_session": {"id": "session-1"}}}
                    }).encode()
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 100,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                return 200, {"content-type": "text/event-stream"}, (
                    'data: {"p":"response/fragments","o":"APPEND","v":'
                    '[{"type":"RESPONSE","content":"<codex_tool_call>{\\"name\\":\\"read_file\\",'
                    '\\"arguments\\":{\\"path\\":\\"main.py\\"}}</codex_tool_call>"}]}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        provider = FakeProvider()
        streaming, response = provider.responses({
            "model": "deepseek-web/chat",
            "input": "inspect main.py",
            "tools": [{
                "type": "function",
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object"},
            }],
        })
        self.assertFalse(streaming)
        self.assertEqual(response["output"][0]["type"], "function_call")
        self.assertEqual(response["output"][0]["name"], "read_file")
        self.assertEqual([item[1] for item in provider.requests], [
            "/api/v0/chat_session/create",
            "/api/v0/chat/create_pow_challenge",
            "/api/v0/chat/completion",
        ])
        self.assertEqual(solved[0]["difficulty"], 100)
        completion = json.loads(provider.requests[-1][2])
        self.assertEqual(completion["chat_session_id"], "session-1")
        self.assertEqual(completion["model_type"], "default")
        pow_value = json.loads(base64.b64decode(provider.requests[-1][3]["x-ds-pow-response"]))
        self.assertEqual(pow_value["answer"], 23)

    def test_provider_recovers_invalid_direct_tool_json_with_continuation(self) -> None:
        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda _challenge: 1)
                self.requests = []
                self.completion_count = 0

            def _request(self, method, upstream_path, body, headers):
                self.requests.append((method, upstream_path, body, dict(headers)))
                if upstream_path.endswith("chat_session/create"):
                    return 200, {}, json.dumps({
                        "data": {"biz_code": 0, "biz_data": {"chat_session": {"id": "session-1"}}}
                    }).encode()
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 1,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                self.completion_count += 1
                if self.completion_count == 1:
                    message_id = "message-bad"
                    answer = '<exec_command>{"cmd":"echo "hello""}</exec_command>'
                else:
                    message_id = "message-good"
                    answer = '<exec_command>{"cmd":"echo \\"hello\\""}</exec_command>'
                escaped = json.dumps(answer)
                return 200, {"content-type": "text/event-stream"}, (
                    f'data: {{"p":"response/message_id","v":"{message_id}"}}\n\n'
                    f'data: {{"p":"response/fragments","o":"APPEND","v":'
                    f'[{{"type":"RESPONSE","content":{escaped}}}]}}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        provider = FakeProvider()
        _streaming, response = provider.responses({
            "model": "deepseek-web/chat",
            "input": "run it",
            "tools": [{
                "type": "function",
                "name": "exec_command",
                "description": "Run a command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            }],
        })
        self.assertEqual(response["output"][0]["type"], "function_call")
        self.assertEqual(response["output"][0]["name"], "exec_command")
        self.assertEqual(json.loads(response["output"][0]["arguments"]), {"cmd": 'echo "hello"'})
        completions = [
            json.loads(body)
            for _method, path, body, _headers in provider.requests
            if path.endswith("/chat/completion")
        ]
        self.assertEqual(len(completions), 2)
        self.assertIsNone(completions[0]["parent_message_id"])
        self.assertEqual(completions[1]["parent_message_id"], "message-bad")
        self.assertIn("TOOL CALL FORMAT ERROR", completions[1]["prompt"])
        self.assertIn("invalid JSON arguments", completions[1]["prompt"])

    def test_provider_uses_web_history_to_keep_thinking_out_of_final_answer(self) -> None:
        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda _challenge: 1)

            def _request(self, method, upstream_path, body, headers):
                if upstream_path.endswith("chat_session/create"):
                    return 200, {}, json.dumps({
                        "data": {"biz_code": 0, "biz_data": {"chat_session": {"id": "session-1"}}}
                    }).encode()
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 1,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                if upstream_path.startswith("/api/v0/chat/history_messages?"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "chat_messages": [{
                                    "message_id": 20,
                                    "role": "ASSISTANT",
                                    "fragments": [
                                        {"type": "THINK", "content": "private planning"},
                                        {"type": "RESPONSE", "content": "visible answer"},
                                        {"type": "TIP", "content": "AI generated"},
                                    ],
                                }]
                            },
                        }
                    }).encode()
                return 200, {"content-type": "text/event-stream"}, (
                    'event: ready\n'
                    'data: {"response_message_id":20,"model_type":"default"}\n\n'
                    'data: {"v":"private planning"}\n\n'
                    'data: {"p":"response/fragments","o":"APPEND","v":'
                    '[{"type":"RESPONSE","content":"visible answer"}]}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        provider = FakeProvider()
        streaming, response = provider.responses({
            "model": "deepseek-web/chat",
            "reasoning": {"effort": "high"},
            "input": "answer me",
        })
        self.assertFalse(streaming)
        self.assertEqual(response["output"][0]["content"][0]["text"], "visible answer")

    def test_provider_reuses_same_deepseek_session_and_sends_only_new_turn(self) -> None:
        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda _challenge: 1)
                self.requests = []
                self.session_count = 0
                self.completion_count = 0

            def _request(self, method, upstream_path, body, headers):
                self.requests.append((method, upstream_path, body, dict(headers)))
                if upstream_path.endswith("chat_session/create"):
                    self.session_count += 1
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {"chat_session": {"id": f"session-{self.session_count}"}},
                        }
                    }).encode()
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 1,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                self.completion_count += 1
                answers = {1: "first answer", 2: "second answer", 3: "third answer"}
                answer = answers[self.completion_count]
                message_id = f"message-{self.completion_count}"
                return 200, {"content-type": "text/event-stream"}, (
                    f'data: {{"p":"response/message_id","v":"{message_id}"}}\n\n'
                    f'data: {{"p":"response/fragments","o":"APPEND","v":'
                    f'[{{"type":"RESPONSE","content":"{answer}"}}]}}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        provider = FakeProvider()
        common = {
            "model": "deepseek-web/chat",
            "instructions": "Follow repo rules.",
            "tools": [{
                "type": "function",
                "name": "read_file",
                "description": "Read one file",
                "parameters": {"type": "object"},
            }],
            "prompt_cache_key": "thread-1",
            "client_metadata": {
                "x-codex-turn-metadata": json.dumps({
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                })
            },
        }
        _streaming, first = provider.responses({
            **common,
            "input": [{"type": "message", "role": "user", "content": "first question"}],
        })
        _streaming, second = provider.responses({
            **common,
            "client_metadata": {
                "x-codex-turn-metadata": json.dumps({
                    "thread_id": "thread-1",
                    "turn_id": "turn-2",
                })
            },
            "input": [
                {"type": "message", "role": "user", "content": "first question"},
                {"type": "message", "role": "assistant", "content": "first answer"},
                {"type": "message", "role": "user", "content": "second question"},
            ],
        })

        self.assertEqual(first["output"][0]["content"][0]["text"], "first answer")
        self.assertEqual(second["output"][0]["content"][0]["text"], "second answer")
        self.assertEqual(provider.session_count, 1)
        completions = [
            json.loads(body)
            for _method, path, body, _headers in provider.requests
            if path.endswith("/chat/completion")
        ]
        self.assertEqual(completions[0]["chat_session_id"], "session-1")
        self.assertIsNone(completions[0]["parent_message_id"])
        self.assertEqual(completions[1]["chat_session_id"], "session-1")
        self.assertEqual(completions[1]["parent_message_id"], "message-1")
        self.assertIn("second question", completions[1]["prompt"])
        self.assertNotIn("first question", completions[1]["prompt"])
        self.assertNotIn("first answer", completions[1]["prompt"])
        self.assertNotIn("Follow repo rules.", completions[1]["prompt"])
        self.assertNotIn("CODEX TOOLS AVAILABLE", completions[1]["prompt"])

        _streaming, third = provider.responses({
            **common,
            "client_metadata": {
                "x-codex-turn-metadata": json.dumps({
                    "thread_id": "thread-1",
                    "turn_id": "turn-3",
                })
            },
            "previous_response_id": second["id"],
            "input": [{"type": "message", "role": "user", "content": "third question"}],
        })
        self.assertEqual(third["output"][0]["content"][0]["text"], "third answer")
        self.assertEqual(provider.session_count, 1)
        completions = [
            json.loads(body)
            for _method, path, body, _headers in provider.requests
            if path.endswith("/chat/completion")
        ]
        self.assertEqual(completions[2]["chat_session_id"], "session-1")
        self.assertEqual(completions[2]["parent_message_id"], "message-2")
        self.assertIn("third question", completions[2]["prompt"])
        self.assertNotIn("second question", completions[2]["prompt"])
        self.assertNotIn("CODEX TOOLS AVAILABLE", completions[2]["prompt"])


if __name__ == "__main__":
    unittest.main()
