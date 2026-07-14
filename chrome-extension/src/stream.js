export async function pumpResponseBody(body, { maxChunkBytes, sendFrame }) {
  if (!body) {
    await sendFrame(0, new Uint8Array(), true);
    return;
  }

  const reader = body.getReader();
  let sequence = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    for (let offset = 0; offset < value.byteLength; offset += maxChunkBytes) {
      const piece = value.slice(offset, Math.min(offset + maxChunkBytes, value.byteLength));
      await sendFrame(sequence, piece, false);
      sequence += 1;
    }
  }

  // Do not retain the latest data chunk while waiting for EOF. Some streaming
  // upstreams deliver their terminal event before keeping the HTTP connection
  // alive briefly; consumers must receive that event immediately.
  await sendFrame(sequence, new Uint8Array(), true);
}
