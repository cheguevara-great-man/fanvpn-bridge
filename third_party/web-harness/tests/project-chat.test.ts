import { expect, test } from "bun:test";
import { defaultConfig, providerConfig } from "../src/config";
import {
  assertChatGptProjectPage,
  isChatGptProjectPageUrl,
  normalizeChatGptProjectUrl,
} from "../src/chatgpt-session";
import {
  ChatGptBrowserWorker,
  chatGptScopedConversationKey,
  resolveBrowserConfig,
} from "../src/adapters/chatgpt-web/browser-worker";

const projectId = "g-p-00000000000000000000000000000001";
const otherProjectId = "g-p-00000000000000000000000000000002";
const project = `https://chatgpt.com/g/${projectId}/project`;
const conversation = `https://chatgpt.com/g/${projectId}/c/12345678-abcd`;

test("project destination is optional, canonical, and propagated to the browser worker", () => {
  expect(normalizeChatGptProjectUrl(undefined)).toBeUndefined();
  expect(normalizeChatGptProjectUrl(` ${project}/ `)).toBe(project);
  const config = { ...defaultConfig(), chatgptProjectUrl: project };
  const provider = providerConfig(config);
  expect(provider.chatgptWeb?.projectUrl).toBe(project);
  expect(resolveBrowserConfig(provider).projectUrl).toBe(project);
  expect(resolveBrowserConfig(providerConfig(defaultConfig())).projectUrl).toBeUndefined();
});

test.each([
  "", null, 12,
  `https://example.com/g/${projectId}/project`,
  `https://chatgpt.com.evil.test/g/${projectId}/project`,
  `http://chatgpt.com/g/${projectId}/project`,
  `https://user:password@chatgpt.com/g/${projectId}/project`,
  "https://chatgpt.com/g/g-p-not-an-id/project",
  "https://chatgpt.com/", conversation,
  `${project}?temporary-chat=true`, `${project}#fragment`,
])("rejects invalid project configuration: %s", value => {
  expect(() => normalizeChatGptProjectUrl(value)).toThrow();
});

test("project ownership rejects temporary, unscoped and unrelated conversations", async () => {
  expect(isChatGptProjectPageUrl(project, project)).toBe(true);
  expect(isChatGptProjectPageUrl(conversation, project)).toBe(false);
  expect(isChatGptProjectPageUrl(conversation, project, true)).toBe(true);
  for (const url of [
    "about:blank", "https://chatgpt.com/?temporary-chat=true",
    "https://chatgpt.com/c/12345678-abcd",
    conversation.replace(projectId, otherProjectId),
    `${conversation}/extra`, `${conversation}?temporary-chat=true`,
  ]) {
    expect(isChatGptProjectPageUrl(url, project, true)).toBe(false);
    await expect(assertChatGptProjectPage({ url: () => url } as any, project, true)).rejects.toThrow("configured project");
  }
});

test("retained conversation keys are stable within a project and separate across destinations", () => {
  const key = "a".repeat(64);
  expect(chatGptScopedConversationKey(key)).toBe(key);
  expect(chatGptScopedConversationKey(undefined, project)).toBeUndefined();
  const scoped = chatGptScopedConversationKey(key, project);
  expect(scoped).toMatch(/^[a-f0-9]{64}$/);
  expect(scoped).not.toBe(key);
  expect(scoped).toBe(chatGptScopedConversationKey(key, `${project}/`));
  expect(scoped).not.toBe(chatGptScopedConversationKey(key, project.replace(projectId, otherProjectId)));
  expect(scoped).not.toBe(chatGptScopedConversationKey("b".repeat(64), project));
});

test("project-name slugs and renames preserve project ownership and retained keys", async () => {
  const namedProject = project.replace(projectId, `${projectId}-webgpt`);
  const namedConversation = conversation.replace(projectId, `${projectId}-webgpt`);
  const renamedConversation = conversation.replace(projectId, `${projectId}-new-project-name`);
  expect(normalizeChatGptProjectUrl(namedProject)).toBe(project);
  expect(isChatGptProjectPageUrl(namedProject, project)).toBe(true);
  expect(isChatGptProjectPageUrl(namedConversation, project, true)).toBe(true);
  expect(isChatGptProjectPageUrl(renamedConversation, namedProject, true)).toBe(true);
  expect(isChatGptProjectPageUrl(namedConversation, project)).toBe(false);
  expect(isChatGptProjectPageUrl(namedConversation.replace(projectId, otherProjectId), project, true)).toBe(false);
  expect(isChatGptProjectPageUrl(namedConversation.replace(projectId, `${projectId}0`), project, true)).toBe(false);
  await expect(assertChatGptProjectPage({ url: () => namedConversation } as any, project, true)).resolves.toBeUndefined();
  expect(chatGptScopedConversationKey("a".repeat(64), namedProject))
    .toBe(chatGptScopedConversationKey("a".repeat(64), project));
});

test("the deployed WebGPT route format belongs to its configured project", () => {
  expect(isChatGptProjectPageUrl(
    `https://chatgpt.com/g/${projectId}-webgpt/c/00000000-0000-4000-8000-000000000003`,
    project,
    true,
  )).toBe(true);
});

function projectFixture(initialUrl: string) {
  let url = initialUrl;
  const navigations: string[] = [];
  const composer = {};
  const hiddenAlert = { filter: () => hiddenAlert, last: () => hiddenAlert, isVisible: async () => false };
  const page = {
    url: () => url,
    goto: async (destination: string) => { url = destination; navigations.push(destination); },
    locator: (selector: string) => selector.startsWith('[role="alert"]')
      ? hiddenAlert
      : { count: async () => 1, nth: () => ({ isVisible: async () => true }) },
  };
  const worker = Object.assign(Object.create(ChatGptBrowserWorker.prototype), {
    config: { projectUrl: project },
    activeComposer: async () => composer,
  });
  return { worker, page, composer, navigations };
}

test("new turns enter the configured project and do not reuse an existing conversation", async () => {
  const f = projectFixture(conversation);
  expect(await f.worker.prepareChatSurface(f.page)).toBe(f.composer);
  expect(f.navigations).toEqual([project]);
});

test("catalog refresh preserves staged messages in the same project conversation", async () => {
  const f = projectFixture(conversation);
  await f.worker.prepareChatSurface(f.page, undefined, true);
  expect(f.navigations).toEqual([]);
});

test("unconfigured destinations retain the previous Temporary Chat preparation", async () => {
  const f = projectFixture("about:blank");
  f.worker.config = {};
  f.worker.prepareTemporaryChatSurface = async () => "temporary-composer";
  expect(await f.worker.prepareChatSurface(f.page)).toBe("temporary-composer");
  expect(f.navigations).toEqual([]);
});

test("project connectors skip the Temporary Chat personalization preflight", async () => {
  const stop = new Error("reached project connector selection");
  let personalizationQueries = 0;
  const page = {
    url: () => project,
    locator: () => ({ filter: () => ({}) }),
    getByText: () => ({}),
    getByRole: () => { personalizationQueries++; throw new Error("Temporary Chat control queried"); },
  };
  const worker = Object.assign(Object.create(ChatGptBrowserWorker.prototype), {
    config: { projectUrl: project, appName: "Codex Native2" },
    activeComposer: async () => { throw stop; },
    clearChatGptComposerState: async () => {},
  });
  await expect(worker.selectConnector(page)).rejects.toBe(stop);
  expect(personalizationQueries).toBe(0);
});

test("submission rejects a project redirect before touching the composer", async () => {
  const f = projectFixture("https://chatgpt.com/");
  f.worker.activeComposer = async () => { throw new Error("outside composer must not be touched"); };
  await expect(f.worker.sendAttachedPrompt(f.page, {})).rejects.toThrow("configured project");
});
