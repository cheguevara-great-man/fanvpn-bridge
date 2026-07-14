import { DatabaseSync } from "node:sqlite";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { isAbsolute, join, normalize, resolve } from "node:path";

const applyChanges = process.argv.includes("--apply");
const allProjects = process.argv.includes("--all-projects");
const projectArgument = process.argv.find((argument) => argument.startsWith("--project="));
const projectRoot = resolve(projectArgument?.slice("--project=".length) || process.cwd());
const codexHome = process.env.CODEX_HOME || join(homedir(), ".codex");
const statePath = join(codexHome, ".codex-global-state.json");
const databasePath = join(codexHome, "state_5.sqlite");

function canonicalPath(value) {
  const withoutDevicePrefix = value.startsWith("\\\\?\\") ? value.slice(4) : value;
  return normalize(resolve(withoutDevicePrefix)).replace(/[\\/]+$/, "").toLowerCase();
}

if (!allProjects && (!isAbsolute(projectRoot) || !existsSync(projectRoot))) {
  throw new Error(`Project path does not exist: ${projectRoot}`);
}
if (!existsSync(statePath) || !existsSync(databasePath)) {
  throw new Error(`Codex state is incomplete under ${codexHome}`);
}

const database = new DatabaseSync(databasePath, { readOnly: true });
let eligibleThreads;
try {
  const activeThreads = database
    .prepare(
      `SELECT id, title, cwd, rollout_path
         FROM threads
        WHERE source = 'vscode' AND archived = 0
        ORDER BY recency_at DESC`,
    )
    .all();
  eligibleThreads = activeThreads.filter((thread) => {
    const threadRoot = resolve(thread.cwd.startsWith("\\\\?\\") ? thread.cwd.slice(4) : thread.cwd);
    return existsSync(threadRoot) &&
      (allProjects || canonicalPath(threadRoot) === canonicalPath(projectRoot));
  });
} finally {
  database.close();
}

const state = JSON.parse(readFileSync(statePath, "utf8"));
const originalState = JSON.stringify(state);
state["thread-workspace-root-hints"] ||= {};
state["thread-writable-roots"] ||= {};
state["projectless-thread-ids"] ||= [];
state["electron-saved-workspace-roots"] ||= [];
state["project-order"] ||= [];
state["active-workspace-roots"] ||= [];
state["electron-persisted-atom-state"] ||= {};
state["electron-persisted-atom-state"]["flat-project-sidebar-preferences-v1"] ||= {};
state["electron-persisted-atom-state"]["flat-project-sidebar-preferences-v1"].chatSortMode =
  "chronological";

const changedThreadIds = [];
for (const thread of eligibleThreads) {
  const threadRoot = allProjects
    ? resolve(thread.cwd.startsWith("\\\\?\\") ? thread.cwd.slice(4) : thread.cwd)
    : projectRoot;
  const hint = state["thread-workspace-root-hints"][thread.id];
  const writableRoots = state["thread-writable-roots"][thread.id] || [];
  const wasProjectless = state["projectless-thread-ids"].includes(thread.id);
  if (
    canonicalPath(hint || threadRoot) !== canonicalPath(threadRoot) ||
    hint === undefined ||
    !writableRoots.some((root) => canonicalPath(root) === canonicalPath(threadRoot)) ||
    wasProjectless
  ) {
    changedThreadIds.push(thread.id);
  }
  state["thread-workspace-root-hints"][thread.id] = threadRoot;
  state["thread-writable-roots"][thread.id] = [threadRoot];
  state["projectless-thread-ids"] = state["projectless-thread-ids"].filter(
    (id) => id !== thread.id,
  );
}

const recoveredProjectRoots = [
  ...new Map(
    eligibleThreads.map((thread) => {
      const root = allProjects
        ? resolve(thread.cwd.startsWith("\\\\?\\") ? thread.cwd.slice(4) : thread.cwd)
        : projectRoot;
      return [canonicalPath(root), root];
    }),
  ).values(),
];
for (const key of [
  "electron-saved-workspace-roots",
  "project-order",
  "active-workspace-roots",
]) {
  for (const root of recoveredProjectRoots) {
    if (!state[key].some((existingRoot) => canonicalPath(existingRoot) === canonicalPath(root))) {
      state[key].push(root);
    }
  }
}

const result = {
  mode: applyChanges ? "applied" : "dry-run",
  scope: allProjects ? "all-existing-projects" : "single-project",
  codexHome,
  projectRoot: allProjects ? null : projectRoot,
  recoveredProjectRoots,
  eligibleThreads: eligibleThreads.map(({ id, title, cwd }) => ({ id, title, cwd })),
  changedThreadIds,
  sidebarSortMode: "chronological",
  stateChanged: JSON.stringify(state) !== originalState,
  backupPath: null,
};

if (applyChanges && result.stateChanged) {
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const backupDirectory = join(codexHome, "recovery-backups", timestamp);
  mkdirSync(backupDirectory, { recursive: true });
  result.backupPath = join(backupDirectory, ".codex-global-state.json.before-project-repair");
  copyFileSync(statePath, result.backupPath);
  const replacementPath = `${statePath}.repair-${process.pid}.tmp`;
  writeFileSync(replacementPath, `${JSON.stringify(state, null, 2)}\n`, "utf8");
  renameSync(replacementPath, statePath);
}

console.log(JSON.stringify(result, null, 2));
