import { expect, test } from "bun:test";
import { bridgeToResponsesSSE, buildResponseJSON } from "../src/bridge";
import type { AdapterEvent } from "../src/types";

async function* completedEvents(chunks = 1): AsyncGenerator<AdapterEvent> {
  for (let index = 0; index < chunks; index++) {
    yield { type: "text_delta", text: `chunk-${index}:` + "x".repeat(2_048) };
  }
  yield { type: "done", endTurn: true };
}

function responseStream(platform: NodeJS.Platform, chunks = 1): ReadableStream<Uint8Array> {
  return bridgeToResponsesSSE(
    completedEvents(chunks),
    "chatgpt-web/test",
    undefined,
    undefined,
    undefined,
    undefined,
    2_000,
    { streamPlatform: platform },
  );
}

test("Responses SSE completes through the Windows push stream", async () => {
  const body = await new Response(responseStream("win32")).text();

  expect(body).toContain("event: response.completed");
  expect(body).toEndWith("data: [DONE]\n\n");
});

test("Responses SSE streams a provisional delta but commits the authoritative final answer", async () => {
  async function* revisedAnswer(): AsyncGenerator<AdapterEvent> {
    yield { type: "text_delta", text: "Old answer", phase: "final_answer" };
    yield { type: "done", endTurn: true, finalText: "New answer" };
  }
  const body = await new Response(bridgeToResponsesSSE(revisedAnswer(), "chatgpt-web/test")).text();
  const events = body.split("\n\n").flatMap(frame => {
    const match = /^event: ([^\n]+)\ndata: ([^\n]+)$/m.exec(frame);
    return match ? [{ type: match[1], data: JSON.parse(match[2]!) }] : [];
  });
  expect(events.find(event => event.type === "response.output_text.delta")?.data.delta).toBe("Old answer");
  expect(events.find(event => event.type === "response.output_text.done")?.data.text).toBe("New answer");
  expect(events.find(event => event.type === "response.content_part.done")?.data.part.text).toBe("New answer");
  expect(events.find(event => event.type === "response.output_item.done")?.data.item.content[0].text).toBe("New answer");
  expect(events.find(event => event.type === "response.completed")?.data.response.output[0].content[0].text).toBe("New answer");
});

test("non-streaming Responses also commits the authoritative final answer", () => {
  const response = buildResponseJSON([
    { type: "text_delta", text: "Old answer", phase: "final_answer" },
    { type: "done", endTurn: true, finalText: "New answer" },
  ], "chatgpt-web/test") as { output: Array<{ content?: Array<{ text: string }> }> };
  expect(response.output[0]?.content?.[0]?.text).toBe("New answer");
});

test("compaction keeps revised final text inside its synthetic item", async () => {
  async function* revisedSummary(): AsyncGenerator<AdapterEvent> {
    yield { type: "text_delta", text: "Old summary", phase: "final_answer" };
    yield { type: "done", endTurn: true, finalText: "New summary" };
  }
  const stream = bridgeToResponsesSSE(revisedSummary(), "chatgpt-web/test", undefined, undefined, undefined, undefined, 2_000, { compaction: true });
  const body = await new Response(stream).text();
  expect(body).toContain("event: response.completed");
  expect(body).toContain('"type":"compaction"');
  expect(body).not.toContain('"type":"message"');
});

test("Darwin SSE remains decodable through Bun.serve under sustained chunking", async () => {
  const server = Bun.serve({
    port: 0,
    fetch() {
      return new Response(responseStream("darwin", 64), {
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          "X-Accel-Buffering": "no",
        },
      });
    },
  });

  try {
    const response = await fetch(`http://127.0.0.1:${server.port}/v1/responses`);
    const body = await response.text();

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("text/event-stream");
    expect(body).toContain("chunk-63:");
    expect(body).toContain("event: response.completed");
    expect(body).toEndWith("data: [DONE]\n\n");
  } finally {
    await server.stop(true);
  }
});
