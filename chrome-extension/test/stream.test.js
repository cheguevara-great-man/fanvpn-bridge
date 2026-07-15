import assert from "node:assert/strict";
import test from "node:test";

import { pumpResponseBody } from "../src/stream.js";

test("forwards a streaming chunk before the upstream closes", async () => {
  let controller;
  const body = new ReadableStream({
    start(value) {
      controller = value;
      value.enqueue(new Uint8Array([1, 2, 3]));
    },
  });
  const frames = [];
  let firstFrameResolve;
  const firstFrame = new Promise((resolve) => {
    firstFrameResolve = resolve;
  });

  const pumping = pumpResponseBody(body, {
    maxChunkBytes: 1024,
    sendFrame: async (sequence, bytes, end) => {
      frames.push({ sequence, bytes: [...bytes], end });
      firstFrameResolve();
    },
  });

  await firstFrame;
  assert.deepEqual(frames, [{ sequence: 0, bytes: [1, 2, 3], end: false }]);

  controller.close();
  await pumping;
  assert.deepEqual(frames[1], { sequence: 1, bytes: [], end: true });
});

test("chunks data immediately and terminates with a separate empty frame", async () => {
  let source;
  const body = new ReadableStream({
    start(controller) {
      source = new Uint8Array([1, 2, 3, 4, 5]);
      controller.enqueue(source);
      controller.close();
    },
  });
  const frames = [];
  const views = [];

  await pumpResponseBody(body, {
    maxChunkBytes: 2,
    sendFrame: async (sequence, bytes, end) => {
      frames.push({ sequence, bytes: [...bytes], end });
      if (!end) views.push(bytes);
    },
  });

  assert.deepEqual(frames, [
    { sequence: 0, bytes: [1, 2], end: false },
    { sequence: 1, bytes: [3, 4], end: false },
    { sequence: 2, bytes: [5], end: false },
    { sequence: 3, bytes: [], end: true },
  ]);
  assert.equal(views[0].buffer, source.buffer);
  assert.equal(views[1].buffer, source.buffer);
  assert.equal(views[2].buffer, source.buffer);
});

test("forwards a whole response chunk without copying it", async () => {
  const source = new Uint8Array([7, 8, 9]);
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(source);
      controller.close();
    },
  });
  let observed;

  await pumpResponseBody(body, {
    maxChunkBytes: 1024,
    sendFrame: async (_sequence, bytes, end) => {
      if (!end) observed = bytes;
    },
  });

  assert.equal(observed, source);
});
