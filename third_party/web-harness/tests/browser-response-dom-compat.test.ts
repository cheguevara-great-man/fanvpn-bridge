import { expect, test } from "bun:test";
import { createContext, runInContext } from "node:vm";
import type { Locator } from "playwright-core";
import { ChatGptBrowserWorker } from "../src/adapters/chatgpt-web/browser-worker";
import { ChatGptMarkdownBuffer, type ChatGptMarkdownSegment } from "../src/adapters/chatgpt-web/markdown";

type Snapshot = {
  visibleText: string;
  markdownSegments: ChatGptMarkdownSegment[];
  completionActionVisible: boolean;
};

// Run the actual page.evaluate callback against a small DOM, without opening or modifying a tab.
async function snapshot(html: string): Promise<Snapshot> {
  const { createWindow } = require("@mixmark-io/domino");
  const window = createWindow(html);
  const prototype = window.HTMLElement.prototype;
  const innerText = Object.getOwnPropertyDescriptor(prototype, "innerText");
  const append = Object.getOwnPropertyDescriptor(prototype, "append");
  Object.defineProperty(prototype, "innerText", { configurable: true, get() { return this.textContent; } });
  Object.defineProperty(prototype, "append", { configurable: true, value(this: HTMLElement, ...nodes: Node[]) {
    nodes.forEach(node => this.appendChild(node));
  } });
  const collections = [window.document.querySelectorAll("div"), window.document.body.children].map(Object.getPrototypeOf);
  const iterators = collections.map(item => Object.getOwnPropertyDescriptor(item, Symbol.iterator));
  for (const item of collections) Object.defineProperty(item, Symbol.iterator, {
    configurable: true, value: Array.prototype[Symbol.iterator],
  });
  try {
    const context = createContext({
      document: window.document, HTMLElement: window.HTMLElement, Element: window.Element,
      Node: window.Node, NodeFilter: window.NodeFilter, performance: { timeOrigin: 1 },
      getComputedStyle: (element: HTMLElement) => ({
        display: element.style.display || "block", visibility: "visible", opacity: "1",
      }),
      MutationObserver: class { observe() {} },
    });
    const locator = {
      evaluate: async (callback: Function, options: unknown) => runInContext(`(${callback.toString()})`, context)(
        window.document.getElementById("turn"), options,
      ),
      page: () => ({ isClosed: () => false }),
    } as unknown as Locator;
    const worker = Object.create(ChatGptBrowserWorker.prototype) as {
      responseDomSnapshot(locator: Locator): Promise<Snapshot>;
    };
    return await worker.responseDomSnapshot(locator);
  } finally {
    collections.forEach((item, index) => {
      if (iterators[index]) Object.defineProperty(item, Symbol.iterator, iterators[index]!);
      else delete item[Symbol.iterator];
    });
    if (innerText) Object.defineProperty(prototype, "innerText", innerText);
    else delete prototype.innerText;
    if (append) Object.defineProperty(prototype, "append", append);
    else delete prototype.append;
  }
}

const toolbar = '<button data-testid="copy-turn-action-button"></button>';
const legacy = `<section id="turn"><div data-message-author-role="assistant"><div class="markdown"><p>ANSWER</p></div></div>${toolbar}</section>`;
const dil = `<section id="turn"><div data-message-author-role="assistant"><div class="puik-root not-prose not-markdown"><div class="hash_DilResponseRoot"><p>ANSWER</p></div></div></div>${toolbar}</section>`;

test("both ChatGPT answer renderers produce one final answer", async () => {
  for (const html of [legacy, dil, dil.replace("hash_DilResponseRoot", "changed_DilResponseRoot"),
    dil.replace("<p>ANSWER</p>", '<p class="markdown">ANSWER</p>')]) {
    const response = await snapshot(html);
    expect(response.visibleText).toBe("ANSWER");
    expect(response.completionActionVisible).toBeTrue();
    const buffer = new ChatGptMarkdownBuffer();
    buffer.observe(response.markdownSegments, 0);
    expect(buffer.finish().markdown).toBe("ANSWER");
  }
});

test("DIL fallback remains assistant-owned and excludes commentary", async () => {
  const userOwned = await snapshot(dil.replace('data-message-author-role="assistant"', 'data-message-author-role="user"'));
  expect(userOwned.visibleText).toBe("");
  expect(userOwned.completionActionVisible).toBeFalse();

  const unrelated = await snapshot(dil.replace("hash_DilResponseRoot", "unrelated-widget"));
  expect(unrelated.visibleText).toBe("");
  const hidden = await snapshot(dil.replace('class="hash_DilResponseRoot"', 'class="hash_DilResponseRoot" style="display:none"'));
  expect(hidden.visibleText).toBe("");

  const commentary = await snapshot(`<section id="turn"><div data-message-author-role="assistant">`
    + '<div data-streaming-response-status><div class="puik-root not-markdown"><div class="hash_DilResponseRoot"><p>THINKING</p></div></div></div>'
    + '<div class="puik-root not-markdown"><div class="hash_DilResponseRoot"><p>ANSWER</p></div></div>'
    + `</div>${toolbar}</section>`);
  expect(commentary.visibleText).toBe("ANSWER");
});
