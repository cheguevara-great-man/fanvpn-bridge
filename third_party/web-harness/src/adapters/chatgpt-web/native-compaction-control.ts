import { COMPACT_PROMPT } from "../../responses/compaction";
import type { CompactionTransactionHandle } from "./compaction-transaction";

export const CODEX_COMPACTION_CONTROL_WIRE_NAME = "codex.control.compaction_handoff";
export const CODEX_ACTIVE_COMPACTION_REQUEST_MARKER = "CODEX_ACTIVE_COMPACTION_REQUEST";

function compactionControlBinding(transaction: CompactionTransactionHandle): string[] {
  return [
    "Submit the complete checkpoint through the attached Codex Native control plane by calling codex_tool_call exactly once with the binding below.",
    "This one-shot control token is valid only for the reserved compaction operation; do not use it with codex_exec, codex_tool_inventory, or any outer Codex tool.",
    "<codex_compaction_control>",
    `turn_token ${transaction.token}`,
    `wire_name ${CODEX_COMPACTION_CONTROL_WIRE_NAME}`,
    `handoff_id ${transaction.handoffId}`,
    "</codex_compaction_control>",
    `Call codex_tool_call exactly once with ${JSON.stringify({
      turn_token: transaction.token,
      wire_name: CODEX_COMPACTION_CONTROL_WIRE_NAME,
      arguments: {
        handoff_id: transaction.handoffId,
        summary: "<complete checkpoint summary>",
      },
    })}.`,
  ];
}

/**
 * Stop an active browser response after Codex requests compaction. If another tool is already
 * queued, it is intercepted before execution and receives this instruction. Otherwise the last
 * canonical tool result stays first and this control is appended as a separate text block, so a
 * model that would otherwise remain generation-running still gets an explicit stop boundary. The
 * retained conversation then receives the sole structured checkpoint request on a clean message.
 */
export function activeCompactionToolResultInstruction(toolExecuted = false): string {
  return [
    `<${CODEX_ACTIVE_COMPACTION_REQUEST_MARKER}>`,
    toolExecuted
      ? "Codex reached its context limit while this Web response was waiting for the tool result above."
      : "Codex reached its context limit before this newly requested tool could be sent for execution. The tool was not executed.",
    toolExecuted
      ? "Consume that canonical result, stop ordinary task work now, call no more tools, and end this Web response normally."
      : "Stop ordinary task work now, call no more tools, and end this Web response normally.",
    "Your entire remaining response must be exactly: Paused for context compaction.",
    "Do not answer the user's question, recap findings, explain the pause, or claim the task is complete. The interrupted task continues after the checkpoint.",
    "Do not create or submit a checkpoint in this response. After it settles, the retained conversation will receive exactly one separate structured compaction handoff request.",
    `</${CODEX_ACTIVE_COMPACTION_REQUEST_MARKER}>`,
  ].join("\n");
}

/**
 * Zero Risk cannot submit a second browser message automatically. When Codex compacts at an
 * already-visible native tool boundary, the same manually submitted response returns the
 * checkpoint through the same Zero Risk request instead.
 */
export function zeroRiskActiveCompactionToolResultInstruction(toolExecuted: boolean): string {
  return [
    `<${CODEX_ACTIVE_COMPACTION_REQUEST_MARKER}>`,
    toolExecuted
      ? "Codex reached its context limit while this Web response was waiting for the tool result above."
      : "Codex reached its context limit before the requested tool could be sent for execution. The tool was not executed.",
    toolExecuted
      ? "Consume that canonical result, stop ordinary task work now, and do not call any more work tools."
      : "Stop ordinary task work now and do not call any more work tools.",
    COMPACT_PROMPT,
    "Call no more work tools. Return only the complete checkpoint summary to Codex with codex_turn_complete.",
    `</${CODEX_ACTIVE_COMPACTION_REQUEST_MARKER}>`,
  ].join("\n");
}

export function structuredCompactionHandoffInstruction(
  transaction: CompactionTransactionHandle,
): string {
  return [
    "Automatic Codex context compaction has started. Stop ordinary task work and do not call any more work tools.",
    COMPACT_PROMPT,
    ...compactionControlBinding(transaction),
    "Keep the checkpoint concise (normally at most 1500 words). Preserve verified findings with exact file/line or log references, completed tool effects, unresolved questions, and the single next concrete action. Distinguish evidence from hypotheses.",
    "Record which files and log ranges were already inspected and the useful conclusions. Tell the continuation to use those findings rather than repeat broad searches or dump the same logs. Do not paste raw logs, long code, previous answers, or obsolete debugging plans into the checkpoint.",
    "After the control call returns submitted=true, call no more tools. The bridge will close this one-purpose Web response after accepting the checkpoint.",
    "The outer bridge accepts compaction only after the structured checkpoint is valid and its owned browser turn has physically settled.",
  ].join("\n");
}
