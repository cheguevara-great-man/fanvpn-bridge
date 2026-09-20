import { ChatGptWebAdapterError } from "./adapter-error";

const CHATGPT_INTERNAL_TOOL_PROTOCOL_PREFIXES = [
  "<|DSML|",
  "<||DSML||",
  "<｜DSML｜",
  "<｜｜DSML｜｜",
  "<锝滐綔DSML锝滐綔",
] as const;

function probeText(value: string): string {
  return value.trimStart();
}

export function chatGptInternalToolProtocolLeak(value: string): boolean {
  const probe = probeText(value);
  return CHATGPT_INTERNAL_TOOL_PROTOCOL_PREFIXES.some(prefix => probe.startsWith(prefix));
}

function couldStillBeProtocolPrefix(value: string): boolean {
  const probe = probeText(value);
  if (!probe) return true;
  return CHATGPT_INTERNAL_TOOL_PROTOCOL_PREFIXES.some(prefix => prefix.startsWith(probe));
}

function protocolLeakError(): ChatGptWebAdapterError {
  return new ChatGptWebAdapterError(
    "ChatGPT exposed internal DSML tool-call markup instead of invoking the Codex Native connector.",
    {
      status: 502,
      errorType: "server_error",
      code: "chatgpt_tool_protocol_leak",
      retryable: true,
    },
  );
}

/**
 * Holds only the tiny prefix needed to distinguish normal Markdown from a leaked ChatGPT internal
 * tool-call frame. Once the answer is known to be ordinary text, streaming proceeds unchanged.
 */
export class ChatGptToolProtocolGuard {
  private pending = "";
  private state: "probing" | "safe" | "leaked" = "probing";

  push(delta: string): string {
    if (!delta || this.state === "leaked") return "";
    if (this.state === "safe") return delta;

    this.pending += delta;
    if (chatGptInternalToolProtocolLeak(this.pending)) {
      this.pending = "";
      this.state = "leaked";
      return "";
    }
    if (couldStillBeProtocolPrefix(this.pending)) return "";

    const visible = this.pending;
    this.pending = "";
    this.state = "safe";
    return visible;
  }

  finish(fullAnswer: string): string {
    if (this.state === "leaked" || chatGptInternalToolProtocolLeak(fullAnswer)) {
      this.pending = "";
      this.state = "leaked";
      throw protocolLeakError();
    }
    if (this.state === "safe" || !this.pending) return "";

    const visible = this.pending;
    this.pending = "";
    this.state = "safe";
    return visible;
  }
}
