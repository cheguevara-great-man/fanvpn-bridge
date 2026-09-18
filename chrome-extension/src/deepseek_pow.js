// PoW protocol compatibility derived from zhu1090093659/deepseek-pp (Apache-2.0).
// The bundled WASM is kept byte-for-byte so DeepSeekHashV1 matches the web client.

const SUPPORTED_ALGORITHM = "DeepSeekHashV1";
const POW_WASM_PATH = "deepseek/sha3_wasm_bg.wasm";
const textEncoder = new TextEncoder();
let powWasm = null;
let powWasmPromise = null;

function validateChallenge(challenge) {
  if (!challenge || challenge.algorithm !== SUPPORTED_ALGORITHM) {
    throw new Error(`Unsupported DeepSeek PoW algorithm: ${challenge?.algorithm || "missing"}`);
  }
  if (!/^[0-9a-f]{64}$/i.test(String(challenge.challenge || ""))) {
    throw new Error("Invalid DeepSeek PoW challenge digest");
  }
  if (!Number.isSafeInteger(challenge.difficulty) || challenge.difficulty <= 0) {
    throw new Error(`Invalid DeepSeek PoW difficulty: ${challenge.difficulty}`);
  }
  if (!Number.isFinite(challenge.expireAt) || challenge.expireAt <= 0) {
    throw new Error(`Invalid DeepSeek PoW expiry: ${challenge.expireAt}`);
  }
}

async function loadWasm() {
  if (powWasm) return powWasm;
  if (!powWasmPromise) {
    powWasmPromise = fetch(chrome.runtime.getURL(POW_WASM_PATH))
      .then((response) => {
        if (!response.ok) throw new Error(`Failed to load DeepSeek PoW WASM: HTTP ${response.status}`);
        return response.arrayBuffer();
      })
      .then((bytes) => WebAssembly.instantiate(bytes, {}))
      .then(({ instance }) => {
        powWasm = instance.exports;
        return powWasm;
      })
      .finally(() => {
        if (!powWasm) powWasmPromise = null;
      });
  }
  return powWasmPromise;
}

function writeString(wasm, value) {
  const bytes = textEncoder.encode(value);
  const ptr = wasm.__wbindgen_export_0(bytes.length, 1);
  new Uint8Array(wasm.memory.buffer).set(bytes, ptr);
  return { ptr, len: bytes.length };
}

function solveWithWasm(wasm, challenge) {
  const retPtr = wasm.__wbindgen_add_to_stack_pointer(-16);
  const target = writeString(wasm, String(challenge.challenge).toLowerCase());
  const prefix = writeString(wasm, `${challenge.salt}_${challenge.expireAt}_`);
  try {
    wasm.wasm_solve(
      retPtr,
      target.ptr,
      target.len,
      prefix.ptr,
      prefix.len,
      challenge.difficulty,
    );
    const view = new DataView(wasm.memory.buffer);
    const status = view.getInt32(retPtr, true);
    const answer = view.getFloat64(retPtr + 8, true);
    if (status !== 1 || !Number.isSafeInteger(answer) || answer < 0) {
      throw new Error("DeepSeek PoW solver returned no valid answer");
    }
    return answer;
  } finally {
    wasm.__wbindgen_add_to_stack_pointer(16);
  }
}

export async function solveDeepSeekPowChallenge(challenge) {
  validateChallenge(challenge);
  return solveWithWasm(await loadWasm(), challenge);
}
