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
  pi.on("before_agent_start", (event, ctx) => {
    // CLI --append-system-prompt would suppress Pi's native APPEND_SYSTEM.md discovery.
    const supplement = process.env.SAPPER_PI_APPEND_SYSTEM;
    if (supplement) {
      event.systemPromptOptions.appendSystemPrompt = [event.systemPromptOptions.appendSystemPrompt, supplement]
        .filter(Boolean).join("\n\n");
    }
    const content = execFileSync("python3", [resolve(root, "lib/sapper.py"), "context"], {
      cwd: ctx.cwd, encoding: "utf8", timeout: 5000, maxBuffer: 65536,
    }).trim();
    if (content) return { message: { customType: "sapper-graph", content, display: false } };
  });
}
