const PRODUCT_METADATA_HEAD_TIMEOUT_MS = 10_000;
const PRODUCT_METADATA_MAX_ATTEMPTS = 2;
const PRODUCT_METADATA_MAX_PREEMPTIONS = 4;
const PRODUCT_METADATA_TOTAL_TIMEOUT_MS = 15_000;
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
    let acquired = false;
    try {
      await this.acquireMetadataSlot(request, parentSignal, urgent);
      acquired = true;
      return await this.invokePreemptible(task, request, parentSignal);
    } finally {
      if (acquired) {
        this.activeMetadataRequests.delete(request);
        this.metadataSlotsTaken -= 1;
      }
      request.cleanup();
      this.notifyWaiters();
    }
  }

  async runCatalogAttempt(task, { parentSignal } = {}) {
    const request = this.createPreemptibleRequest(parentSignal);
    let acquired = false;
    try {
      await this.acquireCatalogSlot(request, parentSignal);
      acquired = true;
      return await this.invokePreemptible(task, request, parentSignal);
    } finally {
      if (acquired) {
        this.activeCatalogRequests.delete(request);
        this.catalogSlotTaken = false;
      }
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
    maxPreemptions = PRODUCT_METADATA_MAX_PREEMPTIONS,
    totalTimeoutMs = PRODUCT_METADATA_TOTAL_TIMEOUT_MS,
    scheduler = defaultScheduler,
    onTiming,
  } = {},
) {
  const timing = {
    startedAt: monotonicNow(),
    attempts: 0,
    preemptions: 0,
    fetchHeadMs: 0,
  };
  const attempt = async (signal, timeout) => {
    timing.attempts += 1;
    const fetchStartedAt = monotonicNow();
    try {
      return await fetchAttempt(url, options, {
        parentSignal: signal,
        fetchImpl,
        headTimeoutMs: timeout,
      });
    } finally {
      timing.fetchHeadMs += elapsedMilliseconds(fetchStartedAt);
    }
  };
  let timingReported = false;
  const reportTiming = () => {
    if (timingReported) return;
    timingReported = true;
    const totalMs = elapsedMilliseconds(timing.startedAt);
    try {
      onTiming?.({
        executor_queue_ms: boundedTiming(Math.max(0, totalMs - timing.fetchHeadMs)),
        fetch_head_ms: boundedTiming(timing.fetchHeadMs),
        attempts: boundedCount(timing.attempts),
        preemptions: boundedCount(timing.preemptions),
      });
    } catch (_error) {
      // Diagnostics must never change request behavior.
    }
  };
  const finish = (response) => {
    reportTiming();
    return response;
  };
  const retryable = isRetryableProductMetadataRequest(options?.method, url);
  const backgroundCatalog = isBackgroundProductCatalogRequest(options?.method, url);
  const urgentMetadata = isUrgentProductMetadataRequest(options?.method, url);
  if (!retryable) {
    try {
      return finish(await scheduler.runPriority(() => attempt(parentSignal, null)));
    } catch (error) {
      reportTiming();
      throw error;
    }
  }

  const deadline = createDeadlineSignal(parentSignal, totalTimeoutMs);
  try {
    let failedAttempts = 0;
    let lastError;
    // Network failures consume maxAttempts. Scheduler preemptions have their
    // own small budget because they are not upstream failures, while the hard
    // total deadline remains the final bound for both kinds of retry.
    while (failedAttempts < maxAttempts) {
      const result = backgroundCatalog
        ? await scheduler.runCatalogAttempt(
            (schedulerSignal) => attempt(schedulerSignal, headTimeoutMs),
            { parentSignal: deadline.signal },
          )
        : await scheduler.runMetadataAttempt(
            (schedulerSignal) => attempt(schedulerSignal, headTimeoutMs),
            { parentSignal: deadline.signal, urgent: urgentMetadata },
          );
      if (deadline.timedOut && !parentSignal?.aborted) throw metadataTimeoutError();
      if (result.value !== undefined) return finish(result.value);
      if (parentSignal?.aborted) throw result.error;
      lastError = result.error;
      if (result.preempted) {
        timing.preemptions += 1;
        if (timing.preemptions >= maxPreemptions) throw metadataTimeoutError();
        continue;
      }
      failedAttempts += 1;
    }
    throw lastError ?? metadataTimeoutError();
  } catch (error) {
    reportTiming();
    if (deadline.timedOut && !parentSignal?.aborted) throw metadataTimeoutError();
    throw error;
  } finally {
    deadline.cleanup();
  }
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
    url.pathname === "/backend-api/plugins/featured" ||
    url.pathname === "/backend-api/connectors/directory/list"
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
  let rejectForAbort;
  const abortPromise = new Promise((_resolve, reject) => {
    rejectForAbort = () => reject(abortError());
    if (controller.signal.aborted) rejectForAbort();
    else controller.signal.addEventListener("abort", rejectForAbort, { once: true });
  });
  try {
    // Chrome normally rejects fetch when its signal is aborted. Promise.race
    // is still required because an extension/network implementation that
    // ignores AbortSignal must not permanently occupy a scheduler slot.
    const fetchPromise = Promise.resolve().then(() =>
      fetchImpl(url, { ...options, signal: controller.signal }),
    );
    return await Promise.race([fetchPromise, abortPromise]);
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
    controller.signal.removeEventListener("abort", rejectForAbort);
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

function metadataTimeoutError() {
  const error = new Error("Timed out waiting for product metadata");
  error.name = "TimeoutError";
  error.code = "REQUEST_TIMEOUT";
  error.retryable = true;
  return error;
}

function createDeadlineSignal(parentSignal, timeoutMs) {
  const controller = new AbortController();
  let timedOut = false;
  const abortFromParent = () => controller.abort(parentSignal?.reason);
  if (parentSignal?.aborted) controller.abort(parentSignal.reason);
  else parentSignal?.addEventListener("abort", abortFromParent, { once: true });
  const timer =
    Number.isFinite(timeoutMs) && timeoutMs > 0
      ? setTimeout(() => {
          timedOut = true;
          controller.abort("product_metadata_deadline");
        }, timeoutMs)
      : null;
  return {
    signal: controller.signal,
    get timedOut() {
      return timedOut;
    },
    cleanup() {
      if (timer !== null) clearTimeout(timer);
      parentSignal?.removeEventListener("abort", abortFromParent);
    },
  };
}

function monotonicNow() {
  return globalThis.performance?.now?.() ?? Date.now();
}

function elapsedMilliseconds(startedAt) {
  return Math.max(0, Math.round(monotonicNow() - startedAt));
}

function boundedTiming(value) {
  return Math.min(3_600_000, Math.max(0, Math.trunc(value)));
}

function boundedCount(value) {
  return Math.min(10_000, Math.max(0, Math.trunc(value)));
}
