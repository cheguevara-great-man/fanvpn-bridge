import { expect, test } from "bun:test";
import { ChatGptWebAdapterError } from "../src/adapters/chatgpt-web/adapter-error";
import {
  chatGptInternalToolProtocolLeak,
  ChatGptToolProtocolGuard,
} from "../src/adapters/chatgpt-web/tool-protocol-guard";

test("recognizes the mojibake DSML frame observed in a browser answer", () => {
  expect(chatGptInternalToolProtocolLeak(
    '<锝滐綔DSML锝滐綔 calls>\n<锝滐綔DSML锝滐綔 invoke name="exec_command">',
  )).toBeTrue();
});

test("holds a split DSML prefix and fails before exposing it", () => {
  const guard = new ChatGptToolProtocolGuard();
  expect(guard.push("<锝滐綔DS")).toBe("");
  expect(guard.push("ML锝滐綔 calls>\n")).toBe("");
  expect(() => guard.finish('<锝滐綔DSML锝滐綔 calls>\n')).toThrow(ChatGptWebAdapterError);
  try {
    guard.finish('<锝滐綔DSML锝滐綔 calls>\n');
  } catch (error) {
    expect(error).toMatchObject({ code: "chatgpt_tool_protocol_leak", retryable: true });
  }
});

test("ordinary Markdown keeps streaming after the prefix is proven safe", () => {
  const guard = new ChatGptToolProtocolGuard();
  expect(guard.push("<p>")).toBe("<p>");
  expect(guard.push("normal answer")).toBe("normal answer");
  expect(guard.finish("<p>normal answer")).toBe("");
});
