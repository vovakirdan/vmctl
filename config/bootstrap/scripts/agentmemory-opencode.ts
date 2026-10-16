// Dispatch to bundled capture hooks in a child with the managed remote environment.
import { spawn } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

const hooks = async ({ directory }: { directory: string }) => {
  const run = join(homedir(), ".local/share/vmctl/agentmemory/run");
  const capture = (name: string, data: Record<string, unknown>) => new Promise<void>((resolve) => {
    const child = spawn(run, ["hook", name], { stdio: ["pipe", "ignore", "ignore"] });
    const timer = setTimeout(() => { child.kill(); resolve(); }, 6000);
    child.on("error", () => { clearTimeout(timer); resolve(); });
    child.on("close", () => { clearTimeout(timer); resolve(); });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify({ cwd: directory, ...data }));
  });
  return {
    event: async ({ event }: { event: { type: string; properties: any } }) => {
      const info = event.properties.info || {};
      const session_id = info.id || event.properties.sessionID;
      if (event.type === "session.created") await capture("session-start", { session_id });
      if (event.type === "session.deleted") await capture("session-end", { session_id });
      if (event.type === "session.idle") await capture("stop", { session_id });
    },
    "tool.execute.after": async (input: any, output: any) => capture("post-tool-use", {
      session_id: input.sessionID, tool_name: input.tool, tool_input: output.args,
      tool_response: output.output,
    }),
    "chat.message": async (input: any, output: any) => capture("prompt-submit", {
      session_id: input.sessionID,
      prompt: (output.parts || []).filter((part: any) => part.type === "text").map((part: any) => part.text).join("\n"),
    }),
  };
};

// OpenCode v2 uses hook registration and an async event stream; v1 uses server hooks.
async function setup(ctx: any) {
  if (typeof ctx?.tool?.hook !== "function" || typeof ctx?.session?.hook !== "function") return;
  const adapter = await hooks({ directory: ctx.location?.directory || process.cwd() });
  await ctx.tool.hook("execute.after", async (event: any) => {
    await adapter["tool.execute.after"](
      { sessionID: event.sessionID, tool: event.tool },
      { args: event.input, output: event.result?.output?.output ?? event.result?.content },
    );
  });
  await ctx.session.hook("prompt", async (event: any) => {
    await adapter["chat.message"](
      { sessionID: event.sessionID }, { parts: [{ type: "text", text: event.prompt?.text || "" }] },
    );
  });
  const controller = new AbortController();
  const registered = new Set<string>();
  void (async () => {
    try {
      for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
        const data = event.data || {};
        const sessionID = data.sessionID;
        if (!sessionID) continue;
        if (!registered.has(sessionID)) {
          registered.add(sessionID);
          await adapter.event({ event: { type: "session.created", properties: { info: { id: sessionID } } } });
        }
        if (event.type === "session.deleted" || event.type === "session.execution.succeeded") {
          await adapter.event({ event: {
            type: event.type === "session.deleted" ? "session.deleted" : "session.idle",
            properties: { sessionID },
          } });
          if (event.type === "session.deleted") registered.delete(sessionID);
        }
      }
    } catch { /* Capture failure must not interrupt the agent. */ }
  })();
  return () => controller.abort();
}

export default { id: "vmctl-agentmemory", setup, server: hooks };
