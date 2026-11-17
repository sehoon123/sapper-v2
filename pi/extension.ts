import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { execFileSync } from "node:child_process";
import { dirname, delimiter, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export default function (pi: ExtensionAPI) {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
  const bin = resolve(root, "bin");
  if (!(process.env.PATH ?? "").split(delimiter).includes(bin)) {
    process.env.PATH = bin + delimiter + (process.env.PATH ?? "");
  }
  pi.on("before_agent_start", (_event, ctx) => {
    const content = execFileSync("python3", [resolve(root, "lib/sapper.py"), "context"], {
      cwd: ctx.cwd, encoding: "utf8", timeout: 5000, maxBuffer: 65536,
    }).trim();
    if (content) return { message: { customType: "sapper-graph", content, display: false } };
  });
  const policy = process.env.SAPPER_PI_POLICY ?? "readonly";
  if (!["readonly", "autonomous"].includes(policy)) throw new Error("Invalid Sapper Pi policy");
  pi.registerCommand("sapper-policy", {
    description: `sapper-policy-v1:${policy}`,
    handler: async (_args, ctx) => { ctx.ui.notify(`Sapper policy: ${policy}`, "info"); },
  });
  pi.on("tool_call", (event) => {
    if (policy !== "autonomous" && !["read", "grep", "find", "ls"].includes(event.toolName)) {
      return { block: true, reason: "Sapper readonly policy denied this tool" };
    }
  });
  pi.on("user_bash", () => {
    if (policy !== "autonomous") {
      return { result: { output: "Sapper readonly policy denied shell execution", exitCode: 1, cancelled: false, truncated: false } };
    }
  });
}
