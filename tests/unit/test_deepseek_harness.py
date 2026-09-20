from __future__ import annotations

import base64
import json
import unittest

from fanvpn_bridge.deepseek_harness import (
    DeepSeekHarnessProvider,
    _encode_pow_response,
    _parse_deepseek_stream,
    _parse_tool_calls,
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
                    }
                ],
            }
        )
        self.assertIn("Follow the repository rules.", prompt)
        self.assertIn("USER:\ninspect it", prompt)
        self.assertIn("TOOL RESULT (call_old):\ncontents", prompt)
        self.assertIn('"name":"read_file"', prompt)
        self.assertIn("Codex, not you, executes tools", prompt)
        self.assertIn("<codex_tool_call>", prompt)
        self.assertIn("Do not use DSML", prompt)
        self.assertIn("The opening tag MUST be exactly `<codex_tool_call>`", prompt)
        self.assertIn('exactly two outer fields: `name` and `arguments`', prompt)
        self.assertIn('<codex_tool_call\\">', prompt)
        self.assertIn("Do not emit an extra `</codex_tool_call>`", prompt)

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

    def test_valid_tool_blocks_become_function_calls_even_with_surrounding_prose(self) -> None:
        text = '<codex_tool_call>{"name":"read_file","arguments":{"path":"a.py"}}</codex_tool_call>'
        calls = _parse_tool_calls(text, {"read_file"})
        self.assertEqual(calls, [{"name": "read_file", "arguments": {"path": "a.py"}}])
        self.assertEqual(
            _parse_tool_calls("I will inspect it first.\n\n" + text, {"read_file"}),
            [{"name": "read_file", "arguments": {"path": "a.py"}}],
        )
        self.assertIsNone(_parse_tool_calls(text, {"different_tool"}))

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


if __name__ == "__main__":
    unittest.main()
