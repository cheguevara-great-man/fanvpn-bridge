const PRODUCT_METADATA_HEAD_TIMEOUT_MS = 10_000;
const PRODUCT_METADATA_MAX_ATTEMPTS = 2;
const PRODUCT_METADATA_QUIET_WINDOW_MS = 150;

/**
 * Keep large, retryable product-catalog reads behind interactive traffic.
 * A newly arriving priority request may preempt a catalog fetch only before
 * its response head is returned, so no partial response is ever exposed to
 * the native host.
 */
export class ProductRequestScheduler {
  constructor({
    quietWindowMs = PRODUCT_METADATA_QUIET_WINDOW_MS,
    metadataConcurrency = 3,
    urgentMetadataConcurrency = metadataConcurrency + 1,
  } = {}) {
    this.quietWindowMs = quietWindowMs;
    this.metadataConcurrency = metadataConcurrency;
    this.urgentMetadataConcurrency = urgentMetadataConcurrency;
    this.priorityRequests = 0;
    this.metadataSlotsTaken = 0;
    this.catalogSlotTaken = false;
    this.activeMetadataRequests = new Set();
    this.activeCatalogRequests = new Set();
    this.waiters = new Set();
  }

  async runPriority(task) {
    this.priorityRequests += 1;
    this.preempt(this.activeMetadataRequests);
    this.preempt(this.activeCatalogRequests);
    this.notifyWaiters();
    try {
      return await task();
    } finally {
      this.priorityRequests -= 1;
      this.notifyWaiters();
    }
  }

  async runMetadataAttempt(task, { parentSignal, urgent = false } = {}) {
    const request = this.createPreemptibleRequest(parentSignal);
    await this.acquireMetadataSlot(request, parentSignal, urgent);
    try {
      return await this.invokePreemptible(task, request, parentSignal);
    } finally {
      this.activeMetadataRequests.delete(request);
      this.metadataSlotsTaken -= 1;
      request.cleanup();
      this.notifyWaiters();
    }
  }

  async runCatalogAttempt(task, { parentSignal } = {}) {
    const request = this.createPreemptibleRequest(parentSignal);
    await this.acquireCatalogSlot(request, parentSignal);
    try {
      return await this.invokePreemptible(task, request, parentSignal);
    } finally {
      this.activeCatalogRequests.delete(request);
      this.catalogSlotTaken = false;
      request.cleanup();
      this.notifyWaiters();
    }
  }

  async acquireMetadataSlot(request, parentSignal, urgent) {
    const concurrency = urgent ? this.urgentMetadataConcurrency : this.metadataConcurrency;
    while (this.priorityRequests > 0 || this.metadataSlotsTaken >= concurrency) {
      await this.waitForChange(parentSignal);
    }
    this.metadataSlotsTaken += 1;
    this.activeMetadataRequests.add(request);
    this.preempt(this.activeCatalogRequests);
    this.notifyWaiters();
  }

  async acquireCatalogSlot(request, parentSignal) {
    while (true) {
      while (
        this.priorityRequests > 0 ||
        this.metadataSlotsTaken > 0 ||
        this.catalogSlotTaken
      ) {
        await this.waitForChange(parentSignal);
      }
      await abortableDelay(this.quietWindowMs, parentSignal);
      if (
        this.priorityRequests === 0 &&
        this.metadataSlotsTaken === 0 &&
        !this.catalogSlotTaken
      ) {
        this.catalogSlotTaken = true;
        this.activeCatalogRequests.add(request);
        return;
      }
    }
  }

  createPreemptibleRequest(parentSignal) {
    const controller = new AbortController();
    const abortFromParent = () => controller.abort();
    if (parentSignal?.aborted) controller.abort();
    else parentSignal?.addEventListener("abort", abortFromParent, { once: true });
    return {
      controller,
      preempted: false,
      cleanup: () => parentSignal?.removeEventListener("abort", abortFromParent),
    };
  }

  async invokePreemptible(task, request, parentSignal) {
    try {
      return {
        value: await task(request.controller.signal),
        preempted: false,
      };
    } catch (error) {
      return {
        error,
        preempted: request.preempted && !parentSignal?.aborted,
      };
    }
  }

  preempt(requests) {
    for (const request of requests) {
      request.preempted = true;
      request.controller.abort();
    }
  }

  waitForChange(parentSignal) {
    if (parentSignal?.aborted) return Promise.reject(abortError());
    return new Promise((resolve, reject) => {
      const cleanup = () => {
        this.waiters.delete(onChange);
        parentSignal?.removeEventListener("abort", onAbort);
      };
      const onChange = () => {
        cleanup();
        resolve();
      };
      const onAbort = () => {
        cleanup();
        reject(abortError());
      };
      this.waiters.add(onChange);
      parentSignal?.addEventListener("abort", onAbort, { once: true });
    });
  }

  notifyWaiters() {
    for (const waiter of [...this.waiters]) waiter();
  }
}

const defaultScheduler = new ProductRequestScheduler();

/**
 * Retry only idempotent ChatGPT product-metadata reads that fail before a
 * response head arrives. Model requests, account control-plane requests and
 * MCP/tool POSTs are priority traffic and are never retried here.
 */
export async function resilientFetch(
  url,
  options,
  {
    parentSignal,
    fetchImpl = globalThis.fetch,
    headTimeoutMs = PRODUCT_METADATA_HEAD_TIMEOUT_MS,
    maxAttempts = PRODUCT_METADATA_MAX_ATTEMPTS,
    scheduler = defaultScheduler,
  } = {},
) {
  const retryable = isRetryableProductMetadataRequest(options?.method, url);
  const backgroundCatalog = isBackgroundProductCatalogRequest(options?.method, url);
  const urgentMetadata = isUrgentProductMetadataRequest(options?.method, url);
  if (!retryable) {
    return scheduler.runPriority(() =>
      fetchAttempt(url, options, {
        parentSignal,
        fetchImpl,
        headTimeoutMs: null,
      }),
    );
  }

  if (!backgroundCatalog) {
    let attempts = 0;
    let lastError;
    while (attempts < maxAttempts) {
      const result = await scheduler.runMetadataAttempt(
        (schedulerSignal) =>
          fetchAttempt(url, options, {
            parentSignal: schedulerSignal,
            fetchImpl,
            headTimeoutMs,
          }),
        { parentSignal, urgent: urgentMetadata },
      );
      if (result.value !== undefined) return result.value;
      if (parentSignal?.aborted) throw result.error;
      if (result.preempted) continue;
      attempts += 1;
      lastError = result.error;
    }
    throw lastError;
  }

  let attempts = 0;
  let lastError;
  while (attempts < maxAttempts) {
    const result = await scheduler.runCatalogAttempt(
      (schedulerSignal) =>
        fetchAttempt(url, options, {
          parentSignal: schedulerSignal,
          fetchImpl,
          headTimeoutMs,
        }),
      { parentSignal },
    );
    if (result.value !== undefined) return result.value;
    if (parentSignal?.aborted) throw result.error;
    if (result.preempted) continue;
    attempts += 1;
    lastError = result.error;
  }
  throw lastError;
}

export function isRetryableProductMetadataRequest(method, value) {
  if (method !== "GET") return false;
  let url;
  try {
    url = new URL(value);
  } catch (_error) {
    return false;
  }
  if (url.protocol !== "https:" || url.hostname !== "chatgpt.com" || url.port) return false;
  return (
    url.pathname === "/backend-api/ps/plugins/list" ||
    url.pathname === "/backend-api/ps/plugins/installed" ||
    url.pathname === "/backend-api/ps/plugins/suggested" ||
    url.pathname === "/backend-api/plugins/featured"
  );
}

export function isBackgroundProductCatalogRequest(method, value) {
  if (method !== "GET") return false;
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      url.hostname === "chatgpt.com" &&
      !url.port &&
      url.pathname === "/backend-api/ps/plugins/list" &&
      url.searchParams.get("scope") === "GLOBAL"
    );
  } catch (_error) {
    return false;
  }
}

export function isUrgentProductMetadataRequest(method, value) {
  if (method !== "GET") return false;
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      url.hostname === "chatgpt.com" &&
      !url.port &&
      (url.pathname === "/backend-api/ps/plugins/suggested" ||
        url.pathname === "/backend-api/plugins/featured")
    );
  } catch (_error) {
    return false;
  }
}

async function fetchAttempt(url, options, { parentSignal, fetchImpl, headTimeoutMs }) {
  const controller = new AbortController();
  let timedOut = false;
  const abortFromParent = () => controller.abort();
  if (parentSignal?.aborted) controller.abort();
  else parentSignal?.addEventListener("abort", abortFromParent, { once: true });
  const timer = headTimeoutMs === null ? null : setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, headTimeoutMs);
  try {
    return await fetchImpl(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (timedOut && !parentSignal?.aborted) {
      const timeout = new Error("Timed out waiting for product metadata response headers");
      timeout.name = "TimeoutError";
      timeout.code = "REQUEST_TIMEOUT";
      timeout.retryable = true;
      throw timeout;
    }
    throw error;
  } finally {
    if (timer !== null) clearTimeout(timer);
    parentSignal?.removeEventListener("abort", abortFromParent);
  }
}

function abortableDelay(milliseconds, signal) {
  if (milliseconds <= 0) {
    return signal?.aborted ? Promise.reject(abortError()) : Promise.resolve();
  }
  if (signal?.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    const onAbort = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      reject(abortError());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function abortError() {
  const error = new Error("Request aborted");
  error.name = "AbortError";
  return error;
}
