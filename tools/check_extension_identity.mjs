import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";

const manifest = JSON.parse(fs.readFileSync(new URL("../chrome-extension/manifest.json", import.meta.url)));
const publicKey = Buffer.from(manifest.key, "base64");
const digest = crypto.createHash("sha256").update(publicKey).digest().subarray(0, 16);
const alphabet = "abcdefghijklmnop";
const extensionId = [...digest]
  .flatMap((byte) => [byte >> 4, byte & 15])
  .map((nibble) => alphabet[nibble])
  .join("");

assert.equal(extensionId, "bgpbajocpomglgdffkgcklhepbcfpbfd");
console.log(`extension identity: ${extensionId}`);
