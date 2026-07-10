/**
 * Offscreen document — makes fetch() calls from a page-like context
 * so that FanVPN's proxy routing applies to the request.
 *
 * Communicates with the service worker via chrome.runtime.sendMessage.
 */

// Keep track of active streaming requests so we can cancel them
const activeReaders = new Map();

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "fetch-request") {
        handleFetch(msg).then(sendResponse).catch(err => {
            sendResponse({ id: msg.id, type: "error", error: err.message || String(err) });
        });
        return true; // keep the message channel open for async response
    }

    if (msg.type === "cancel-request") {
        const reader = activeReaders.get(msg.id);
        if (reader) {
            try { reader.cancel(); } catch (_) { /* ignore */ }
            activeReaders.delete(msg.id);
        }
        sendResponse({ id: msg.id, type: "cancelled" });
        return true;
    }
});

/**
 * Encode an ArrayBuffer to base64.
 */
function arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let i = 0; i < bytes.byteLength; i++) {
        binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
}

/**
 * Decode base64 to Uint8Array.
 */
function base64ToBytes(b64) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
}

/**
 * Check if content type indicates a streaming response.
 */
function isStreamContentType(contentType) {
    if (!contentType) return false;
    const ct = contentType.toLowerCase();
    return ct.includes("text/event-stream") || ct.includes("application/x-ndjson");
}

/**
 * Main fetch handler.
 */
async function handleFetch(msg) {
    const { id, method, url, headers, body } = msg;
    if (!id || !url) {
        return { id, type: "error", error: "Missing id or url" };
    }

    console.log("[FanVPN Offscreen] →", method, url);

    try {
        const fetchOpts = {
            method: method || "GET",
            headers: headers || {},
        };

        // Attach body for non-GET/HEAD
        if (body && method !== "GET" && method !== "HEAD") {
            fetchOpts.body = base64ToBytes(body).buffer;
        }

        const response = await fetch(url, fetchOpts);
        const contentType = response.headers.get("content-type") || "";

        if (isStreamContentType(contentType)) {
            return await handleStream(id, response);
        } else {
            return await handleComplete(id, response);
        }
    } catch (err) {
        console.error("[FanVPN Offscreen] Error:", err.message);
        return { id, type: "error", error: err.message || String(err) };
    }
}

/**
 * Handle a complete (non-streaming) response.
 */
async function handleComplete(id, response) {
    const headers = {};
    for (const [k, v] of response.headers.entries()) {
        headers[k] = v;
    }

    let body = "";
    try {
        const arrayBuf = await response.arrayBuffer();
        body = arrayBufferToBase64(arrayBuf);
    } catch (err) {
        return { id, type: "error", error: "Failed to read response body" };
    }

    console.log("[FanVPN Offscreen] ←", response.status,
        body.length > 0 ? `(${Math.round(body.length * 0.75)} bytes)` : "");

    return {
        id,
        type: "complete",
        status: response.status,
        statusText: response.statusText,
        headers,
        body,
    };
}

/**
 * Handle a streaming (SSE/NDJSON) response.
 * For streams, we use multiple sendMessage calls — one per chunk + a final done.
 * But since sendMessage requires a response, the service worker must
 * respond to each chunk message. We use a simpler approach:
 * send the status first, then stream chunks via individual messages.
 */
async function handleStream(id, response) {
    console.log("[FanVPN Offscreen] ←", response.status, "(stream)");

    // Send initial status
    const headers = {};
    for (const [k, v] of response.headers.entries()) {
        headers[k] = v;
    }

    // Collect all chunks into a single response.
    // For true streaming, we'd need postMessage streaming,
    // but chrome.runtime.sendMessage is request-response, not push.
    // Instead, we read the whole stream and return as a "stream" response
    // with multiple chunks encoded as a JSON array of base64 strings.
    try {
        const reader = response.body.getReader();
        activeReaders.set(id, reader);

        const chunks = [];
        let totalBytes = 0;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            chunks.push(arrayBufferToBase64(
                value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength)
            ));
            totalBytes += value.byteLength;

            // For very large streams, send intermediate chunks
            // to avoid memory issues
            if (totalBytes > 256 * 1024) { // 256KB batch
                chrome.runtime.sendMessage({
                    id,
                    type: "stream-batch",
                    status: response.status,
                    headers,
                    chunks,
                }).catch(() => { /* ignore if no listener */ });
                chunks.length = 0;
                totalBytes = 0;
            }
        }

        activeReaders.delete(id);

        // Send remaining chunks
        if (chunks.length > 0) {
            chrome.runtime.sendMessage({
                id,
                type: "stream-batch",
                status: response.status,
                headers,
                chunks,
            }).catch(() => { /* ignore */ });
        }

    } catch (err) {
        activeReaders.delete(id);
        console.error("[FanVPN Offscreen] Stream read error:", err.message);
        chrome.runtime.sendMessage({
            id,
            type: "stream_error",
            error: err.message || "Stream read error",
        }).catch(() => { /* ignore */ });
    }

    // Return a completion marker — the actual streaming data
    // was sent via separate messages above.
    return { id, type: "stream-init", status: response.status, headers };
}
