import { DatabaseSync } from "node:sqlite";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const codexHome = process.env.CODEX_HOME || join(homedir(), ".codex");
const databasePath = join(codexHome, "state_5.sqlite");
const database = new DatabaseSync(databasePath, { readOnly: true });
const showThreads = process.argv.includes("--threads");
const showGlobalState = process.argv.includes("--global-state");
const threadIdArgument = process.argv.find((argument) => argument.startsWith("--thread-id="));
const selectedThreadId = threadIdArgument?.slice("--thread-id=".length);

function redact(value, key = "") {
  if (/(token|secret|password|cookie|authorization)/i.test(key) && value != null) {
    return "<redacted>";
  }
  if (Array.isArray(value)) return value.map((item) => redact(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([childKey, child]) => [childKey, redact(child, childKey)]),
    );
  }
  return value;
}

try {
  const tables = database
    .prepare("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name")
    .all()
    .map(({ name, sql }) => ({
      name,
      columns: database.prepare(`PRAGMA table_info(${JSON.stringify(name)})`).all(),
      sql,
    }));
  const threadSummary = showThreads || selectedThreadId
    ? database
        .prepare(
          `SELECT id, title, cwd, source, model_provider, model, cli_version,
                  has_user_event, archived, archived_at, rollout_path,
                  created_at, updated_at, recency_at, history_mode
             FROM threads
            WHERE (? IS NULL OR id = ?)
            ORDER BY recency_at DESC, updated_at DESC`,
        )
        .all(selectedThreadId || null, selectedThreadId || null)
        .map((thread) => ({
          ...thread,
          rollout_exists: existsSync(thread.rollout_path),
        }))
    : undefined;
  const cwdSummary = showThreads
    ? database
        .prepare(
          `SELECT cwd, archived, source, model_provider, COUNT(*) AS count
             FROM threads
            GROUP BY cwd, archived, source, model_provider
            ORDER BY cwd, archived`,
        )
        .all()
    : undefined;
  const sessionIndex = showThreads
    ? readFileSync(join(codexHome, "session_index.jsonl"), "utf8")
        .split(/\r?\n/)
        .filter(Boolean)
        .map((line) => JSON.parse(line))
    : undefined;
  const globalState = showGlobalState
    ? redact(JSON.parse(readFileSync(join(codexHome, ".codex-global-state.json"), "utf8")))
    : undefined;
  console.log(
    JSON.stringify(
      {
        codexHome,
        databasePath,
        tables: showThreads || showGlobalState || selectedThreadId ? undefined : tables,
        cwdSummary,
        threadSummary,
        sessionIndex,
        globalState,
      },
      null,
      2,
    ),
  );
} finally {
  database.close();
}
