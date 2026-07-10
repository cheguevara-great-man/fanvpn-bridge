/**
 * FanVPN AI Bridge — Chrome Extension Service Worker (MV3)
 *
 * Maintains a Native Messaging connection to the bridge host.
 * Delegates HTTP requests to an OFFSCREEN DOCUMENT so that
 * fetch() runs in a page-like context — this allows FanVPN's
 * proxy routing to apply (service worker fetch is often bypassed).
 *
 * Architecture:
 *   Native Host ← NM → Service Worker ← sendMessage → Offscreen Doc ← fetch → API
 */

const NATIVE_HOST_NAME = "com.fanvpn.bridge";
const RECONNECT_DELAY_MS = 2000;
const KEEPALIVE_INTERVAL_MS = 25000;

let port = null;
let reconnectTimer = null;
let keepAliveTimer = null;
let offscreenReady = false;

// ── Connection management ──────────────────────────────────────────────

function connect() {
    if (port) {
        try { port.disconnect(); } catch (_) { /* ignore */ }
        port = null;
    }

    try {
        port = chrome.runtime.connectNative(NATIVE_HOST_NAME);
        console.log("[FanVPN Bridge] Connected to native host");

        port.onMessage.addListener(handleMessage);
        port.onDisconnect.addListener(onDisconnect);

        startKeepAlive();
        ensureOffscreen();
    } catch (err) {
        console.error("[FanVPN Bridge] Connection failed:", err.message);
        scheduleReconnect();
    }
}

function onDisconnect() {
    console.warn("[FanVPN Bridge] Disconnected" +
        (chrome.runtime.lastError ? ": " + chrome.runtime.lastError.message : ""));
    port = null;
    stopKeepAlive();
    scheduleReconnect();
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    console.log("[FanVPN Bridge] Reconnecting in", RECONNECT_DELAY_MS / 1000, "s...");
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
    }, RECONNECT_DELAY_MS);
}

// ── Keep-alive ─────────────────────────────────────────────────────────

function startKeepAlive() {
    stopKeepAlive();
    keepAliveTimer = setInterval(() => {
        if (port) {
            try { port.postMessage({ type: "ping" }); } catch (_) { /* */ }
        }
    }, KEEPALIVE_INTERVAL_MS);
}

function stopKeepAlive() {
    if (keepAliveTimer) {
        clearInterval(keepAliveTimer);
        keepAliveTimer = null;
    }
}

// ── Offscreen document management ───────────────────────────────────────

async function ensureOffscreen() {
    if (offscreenReady) return;

    // Check if already exists
    const clients = await chrome.offscreen?.hasDocument?.();
    if (clients) {
        offscreenReady = true;
        return;
    }

    try {
        await chrome.offscreen.createDocument({
            url: "offscreen.html",
            reasons: ["DOM_SCRAPING"],  // we just need a page context for fetch
            justification: "Make fetch requests in page context for FanVPN proxy",
        });
        offscreenReady = true;
        console.log("[FanVPN Bridge] Offscreen document created");
    } catch (err) {
        console.error("[FanVPN Bridge] Failed to create offscreen:", err.message);
        // Fallback: try direct fetch in service worker
    }
}

// ── Incoming messages from native host ──────────────────────────────────

async function handleMessage(msg) {
    if (msg.type === "pong") return;
    if (msg.type === "ping") {
        safePostNative({ type: "pong" });
        return;
    }

    const { id, method, url, headers } = msg;
    if (!id || !url) {
        console.warn("[FanVPN Bridge] Malformed request, missing id/url");
        return;
    }

    console.log("[FanVPN Bridge] →", method, url);

    // If offscreen is available, use it (page context → FanVPN works)
    if (offscreenReady) {
        try {
            const response = await chrome.runtime.sendMessage({
                type: "fetch-request",
                id,
                method: method || "GET",
                url,
                headers: headers || {},
                body: msg.body,
            });
            // Forward response back to native host
            forwardToNative(response);
        } catch (err) {
            console.error("[FanVPN Bridge] Offscreen fetch failed:", err.message);
            // Fallback to direct fetch
            await directFetch(msg);
        }
    } else {
        // No offscreen — use direct fetch (might not go through FanVPN)
        await directFetch(msg);
    }
}

/**
 * Forward a response from the offscreen document to the native host.
 * Handles stream-init responses by starting to listen for stream-batch messages.
 */
function forwardToNative(response) {
    if (!response) return;

    if (response.type === "stream-init") {
        // Stream finished — chunks were already sent as stream-batch messages.
        // Send "done" so the native host can complete the HTTP response.
        console.log("[FanVPN Bridge] Stream complete for", response.id);
        safePostNative({ id: response.id, type: "done" });
        return;
    }

    safePostNative(response);
}

/**
 * Fallback: direct fetch in service worker (may bypass FanVPN).
 */
async function directFetch(msg) {
    const { id, method, url, headers } = msg;

    try {
        const fetchOpts = {
            method: method || "GET",
            headers: headers || {},
        };

        if (msg.body && method !== "GET" && method !== "HEAD") {
            fetchOpts.body = base64ToBytes(msg.body).buffer;
        }

        const response = await fetch(url, fetchOpts);
        const contentType = response.headers.get("content-type") || "";

        if (contentType.includes("text/event-stream") || contentType.includes("application/x-ndjson")) {
            // Stream response
            const respHeaders = {};
            for (const [k, v] of response.headers.entries()) respHeaders[k] = v;
            safePostNative({ id, type: "stream", status: response.status, headers: respHeaders });

            const reader = response.body.getReader();
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                safePostNative({
                    id,
                    type: "stream",
                    body: arrayBufferToBase64(
                        value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength)
                    ),
                });
            }
            safePostNative({ id, type: "done" });
        } else {
            const respHeaders = {};
            for (const [k, v] of response.headers.entries()) respHeaders[k] = v;
            let body = "";
            try {
                body = arrayBufferToBase64(await response.arrayBuffer());
            } catch (_) { /* empty body */ }

            safePostNative({
                id,
                type: "complete",
                status: response.status,
                statusText: response.statusText,
                headers: respHeaders,
                body,
            });
        }
    } catch (err) {
        safePostNative({ id, type: "error", error: err.message || String(err) });
    }
}

// ── Messages FROM offscreen document ────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, sender) => {
    // Handle stream-batch messages from offscreen document
    if (msg.type === "stream-batch") {
        const { id, status, headers, chunks } = msg;

        // First batch includes status + headers
        if (status && headers) {
            safePostNative({ id, type: "stream", status, headers });
        }

        // Send each chunk
        if (chunks && chunks.length > 0) {
            for (const chunk of chunks) {
                safePostNative({ id, type: "stream", body: chunk });
            }
        }

        return; // no async response needed
    }

    if (msg.type === "stream_error") {
        safePostNative({
            id: msg.id,
            type: "stream_error",
            error: msg.error,
        });
        return;
    }
});

// ── Helpers ─────────────────────────────────────────────────────────────

function safePostNative(msg) {
    if (!port) return;
    try {
        port.postMessage(msg);
    } catch (err) {
        console.warn("[FanVPN Bridge] Post failed:", err.message);
    }
}

function arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let i = 0; i < bytes.byteLength; i++) {
        binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
}

function base64ToBytes(b64) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
}

// ── Startup ────────────────────────────────────────────────────────────

connect();
