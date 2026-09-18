# DeepSeek Web Harness

This branch adds DeepSeek Web as a peer provider beside WebGPT and the Google
account provider. It does not replace or wrap the existing WebHarness.

## Architecture

```text
Codex Responses request
        |
        v
Hybrid /v1/responses
        |
        +-- chatgpt-web/*  -> existing WebHarness (unchanged)
        +-- gemini-*       -> existing Gemini account provider
        +-- deepseek-web/* -> DeepSeekHarnessProvider
                                   |
                                   +-- create web chat session
                                   +-- request PoW challenge
                                   +-- solve DeepSeekHashV1 in Chrome/WASM
                                   +-- submit completion
                                   +-- decode DeepSeek SSE
                                   +-- emit OpenAI Responses events
```

The DeepSeek provider sends its web API traffic through the existing Native
Host -> Chrome offscreen egress path. A content script on `chat.deepseek.com`
reads the signed-in page's `userToken`; the background service worker injects
that bearer token only into DeepSeek API requests. The token is not persisted
by Browser AI Bridge and is not sent to Codex.

DeepSeek Web's proof of work is solved in the extension with the compatible
`DeepSeekHashV1` WASM from DeepSeek++ (Apache-2.0). See
`third_party/deepseek-pp/` for attribution.

## Models

- `deepseek-web/chat` -> DeepSeek web `default` model mode
- `deepseek-web/reasoner` -> DeepSeek web `expert` mode with thinking enabled

The models are included in the Hybrid model catalog by
`tools/refresh_model_catalog.ps1`.

## Login and use

1. Build/register this branch's Native Host in the normal Browser AI Bridge
   workflow and reload this branch's unpacked `chrome-extension`.
2. Open `https://chat.deepseek.com` in the same Chrome profile and sign in.
3. Keep a DeepSeek tab available. The extension can recover the token from the
   tab after a service-worker restart, so the credential does not need to be
   written to disk.
4. Refresh the model catalog and select `deepseek-web/chat` or
   `deepseek-web/reasoner` in Codex Hybrid mode.

If the DeepSeek tab is not signed in, the provider returns
`deepseek_auth_required` instead of asking for a token manually.

## Codex tools

DeepSeek Web does not expose Codex's Responses function-calling protocol.
Codex therefore remains the tool executor. The adapter sends the available
function schemas to DeepSeek with a strict `<codex_tool_call>...</codex_tool_call>`
wire format, validates the requested tool name/JSON arguments, and converts a
valid request into a Responses `function_call`. Tool results from Codex are
serialized back into the next DeepSeek prompt.

## Current scope

- Text turns and Codex function tools are supported.
- DeepSeek reasoning text is kept separate and is not exposed as answer text.
- Image/file upload is not yet bridged; image inputs are represented as an
  explicit omission marker in the text prompt.
- The DeepSeek custom SSE stream is currently buffered before Responses events
  are emitted. This favors correct tool-call detection over first-token latency.
