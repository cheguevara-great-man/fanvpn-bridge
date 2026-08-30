import assert from "node:assert/strict";
import test from "node:test";

import { applyBusyState } from "../src/popup_controls.js";

test("busy state tolerates removed optional controls", () => {
  const mode = { disabled: false };
  const serverRoute = { disabled: false };
  assert.doesNotThrow(() => applyBusyState(
    [mode, null, undefined],
    [serverRoute],
    { busy: true, serverRouteAvailable: true },
  ));
  assert.equal(mode.disabled, true);
  assert.equal(serverRoute.disabled, true);
});

test("an unavailable optional server route never blocks Codex modes", () => {
  const mode = { disabled: true };
  const serverRoute = { disabled: true };
  applyBusyState(
    [mode],
    [serverRoute],
    { busy: false, serverRouteAvailable: false },
  );
  assert.equal(mode.disabled, false);
  assert.equal(serverRoute.disabled, true);
});
