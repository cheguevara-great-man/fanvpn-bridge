import { DatabaseSync } from "node:sqlite";
import { homedir } from "node:os";
import { join } from "node:path";

const codexHome = process.env.CODEX_HOME || join(homedir(), ".codex");
const databasePath = join(codexHome, "logs_2.sqlite");
const database = new DatabaseSync(databasePath, { readOnly: true });
const showErrors = process.argv.includes("--errors");

try {
  const tables = database
    .prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    .all()
    .map(({ name }) => ({
      name,
      columns: database.prepare(`PRAGMA table_info(${JSON.stringify(name)})`).all(),
    }));
  const logs = showErrors
    ? database
        .prepare(
          `SELECT id, ts, level, target, module_path, file, line, thread_id
             FROM logs
            WHERE level IN ('ERROR', 'WARN')
            ORDER BY id DESC
            LIMIT 250`,
        )
        .all()
    : undefined;
  console.log(
    JSON.stringify({ codexHome, databasePath, tables: showErrors ? undefined : tables, logs }, null, 2),
  );
} finally {
  database.close();
}
