import assert from "node:assert/strict";
import test from "node:test";

import {
  isBackgroundProductCatalogRequest,
  isRetryableProductMetadataRequest,
  isUrgentProductMetadataRequest,
  ProductRequestScheduler,
  resilientFetch,
} from "../src/resilient_fetch.js";

const catalogUrl = "https://chatgpt.com/backend-api/ps/plugins/list?scope=GLOBAL";
const installedUrl = "https://chatgpt.com/backend-api/ps/plugins/installed?scope=GLOBAL";

test("recognizes only idempotent ChatGPT product metadata requests", () => {
  assert.equal(isRetryableProductMetadataRequest("GET", catalogUrl), true);
  assert.equal(
    isRetryableProductMetadataRequest(
      "GET",
      "https://chatgpt.com/backend-api/ps/plugins/installed?scope=GLOBAL",
    ),
    true,
  );
  assert.equal(isRetryableProductMetadataRequest("POST", catalogUrl), false);
  assert.equal(
    isRetryableProductMetadataRequest("GET", "https://chatgpt.com/backend-api/codex/responses"),
    false,
  );
  assert.equal(
    isUrgentProductMetadataRequest(
      "GET",
      "https://chatgpt.com/backend-api/ps/plugins/suggested?scope=GLOBAL",
    ),
    true,
  );
  assert.equal(isUrgentProductMetadataRequest("GET", installedUrl), false);
  assert.equal(
    isRetryableProductMetadataRequest("GET", "https://attacker.example/backend-api/ps/plugins/list"),
    false,
  );
  assert.equal(isBackgroundProductCatalogRequest("GET", catalogUrl), true);
  assert.equal(
    isBackgroundProductCatalogRequest(
      "GET",
      "https://chatgpt.com/backend-api/ps/plugins/installed?scope=GLOBAL",
    ),
    false,
  );
});

test("retries a stalled catalog read before the Codex client timeout", async () => {
  let calls = 0;
  const fetchImpl = async (_url, options) => {
    calls += 1;
    if (calls === 1) {
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener(
          "abort",
          () => reject(new DOMException("aborted", "AbortError")),
          { once: true },
        );
      });
    }
    return new Response('{"plugins":[]}', { status: 200 });
  };
  const response = await resilientFetch(
    catalogUrl,
    { method: "GET" },
    { fetchImpl, headTimeoutMs: 5 },
  );
  assert.equal(response.status, 200);
  assert.equal(calls, 2);
});

test("retries a transient pre-header network failure for metadata", async () => {
  let calls = 0;
  const response = await resilientFetch(
    catalogUrl,
    { method: "GET" },
    {
      fetchImpl: async () => {
        calls += 1;
        if (calls === 1) throw new TypeError("Failed to fetch");
        return new Response("{}", { status: 200 });
      },
      headTimeoutMs: 50,
    },
  );
  assert.equal(response.status, 200);
  assert.equal(calls, 2);
});

test("never retries POST requests", async () => {
  let calls = 0;
  await assert.rejects(
    resilientFetch(
      "https://chatgpt.com/backend-api/ps/mcp",
      { method: "POST" },
      {
        fetchImpl: async () => {
          calls += 1;
          throw new TypeError("Failed to fetch");
        },
        headTimeoutMs: 5,
      },
    ),
  );
  assert.equal(calls, 1);
});

test("parent cancellation stops metadata retries", async () => {
  let calls = 0;
  let markStarted;
  const started = new Promise((resolve) => {
    markStarted = resolve;
  });
  const parent = new AbortController();
  const pending = resilientFetch(
    catalogUrl,
    { method: "GET" },
    {
      parentSignal: parent.signal,
      fetchImpl: async (_url, options) => {
        calls += 1;
        markStarted();
        return new Promise((_resolve, reject) => {
          options.signal.addEventListener(
            "abort",
            () => reject(new DOMException("aborted", "AbortError")),
            { once: true },
          );
        });
      },
      headTimeoutMs: 100,
    },
  );
  await started;
  parent.abort();
  await assert.rejects(pending);
  assert.equal(calls, 1);
});

test("priority traffic preempts a stalled catalog read and lets it resume", async () => {
  const scheduler = new ProductRequestScheduler({ quietWindowMs: 0 });
  let catalogCalls = 0;
  let markCatalogStarted;
  const catalogStarted = new Promise((resolve) => {
    markCatalogStarted = resolve;
  });
  const fetchImpl = async (url, options) => {
    if (url === catalogUrl) {
      catalogCalls += 1;
      if (catalogCalls === 1) {
        markCatalogStarted();
        return new Promise((_resolve, reject) => {
          options.signal.addEventListener(
            "abort",
            () => reject(new DOMException("preempted", "AbortError")),
            { once: true },
          );
        });
      }
      return new Response('{"plugins":[]}', { status: 200 });
    }
    return new Response('{"statsigPayload":"{}"}', { status: 200 });
  };

  const catalog = resilientFetch(
    catalogUrl,
    { method: "GET" },
    { fetchImpl, headTimeoutMs: 1_000, scheduler },
  );
  await catalogStarted;
  const bootstrap = await resilientFetch(
    "https://chatgpt.com/backend-api/wham/statsig/bootstrap",
    { method: "POST" },
    { fetchImpl, scheduler },
  );
  const catalogResponse = await catalog;

  assert.equal(bootstrap.status, 200);
  assert.equal(catalogResponse.status, 200);
  assert.equal(catalogCalls, 2);
});

test("priority traffic preempts plugin status reads and lets them resume", async () => {
  const scheduler = new ProductRequestScheduler({ quietWindowMs: 0 });
  let metadataCalls = 0;
  let markMetadataStarted;
  const metadataStarted = new Promise((resolve) => {
    markMetadataStarted = resolve;
  });
  const fetchImpl = async (url, options) => {
    if (url === installedUrl) {
      metadataCalls += 1;
      if (metadataCalls === 1) {
        markMetadataStarted();
        return new Promise((_resolve, reject) => {
          options.signal.addEventListener(
            "abort",
            () => reject(new DOMException("preempted", "AbortError")),
            { once: true },
          );
        });
      }
      return new Response("{}", { status: 200 });
    }
    return new Response('{"statsigPayload":"{}"}', { status: 200 });
  };

  const metadata = resilientFetch(
    installedUrl,
    { method: "GET" },
    { fetchImpl, headTimeoutMs: 1_000, scheduler },
  );
  await metadataStarted;
  const bootstrap = await resilientFetch(
    "https://chatgpt.com/backend-api/wham/statsig/bootstrap",
    { method: "POST" },
    { fetchImpl, scheduler },
  );
  const metadataResponse = await metadata;

  assert.equal(bootstrap.status, 200);
  assert.equal(metadataResponse.status, 200);
  assert.equal(metadataCalls, 2);
});

test("plugin status reads have a bounded middle-priority pool", async () => {
  const scheduler = new ProductRequestScheduler({
    quietWindowMs: 0,
    metadataConcurrency: 2,
  });
  let active = 0;
  let maxActive = 0;
  const fetchImpl = async () => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    await new Promise((resolve) => setTimeout(resolve, 5));
    active -= 1;
    return new Response("{}", { status: 200 });
  };

  await Promise.all(
    Array.from({ length: 5 }, (_value, index) =>
      resilientFetch(
        `${installedUrl}&request=${index}`,
        { method: "GET" },
        { fetchImpl, scheduler },
      ),
    ),
  );

  assert.equal(maxActive, 2);
});

test("suggested plugins can use the reserved metadata slot", async () => {
  const scheduler = new ProductRequestScheduler({
    quietWindowMs: 0,
    metadataConcurrency: 2,
    urgentMetadataConcurrency: 3,
  });
  let active = 0;
  let maxActive = 0;
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const fetchImpl = async () => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    await gate;
    active -= 1;
    return new Response("{}", { status: 200 });
  };

  const installed = [0, 1].map((index) =>
    resilientFetch(
      `${installedUrl}&request=${index}`,
      { method: "GET" },
      { fetchImpl, scheduler },
    ),
  );
  await new Promise((resolve) => setTimeout(resolve, 0));
  const suggested = resilientFetch(
    "https://chatgpt.com/backend-api/ps/plugins/suggested?scope=GLOBAL",
    { method: "GET" },
    { fetchImpl, scheduler },
  );
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(maxActive, 3);
  release();
  await Promise.all([...installed, suggested]);
});

test("catalog reads use one background slot", async () => {
  const scheduler = new ProductRequestScheduler({ quietWindowMs: 0 });
  let active = 0;
  let maxActive = 0;
  const fetchImpl = async () => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    await new Promise((resolve) => setTimeout(resolve, 5));
    active -= 1;
    return new Response("{}", { status: 200 });
  };

  await Promise.all([
    resilientFetch(catalogUrl, { method: "GET" }, { fetchImpl, scheduler }),
    resilientFetch(
      "https://chatgpt.com/backend-api/ps/plugins/list?scope=GLOBAL&page_token=next",
      { method: "GET" },
      { fetchImpl, scheduler },
    ),
  ]);

  assert.equal(maxActive, 1);
});
