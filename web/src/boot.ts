import type { Boot } from "./types";

export function readBoot(): Boot {
  const node = document.getElementById("flakepilot-boot");
  const raw = node?.textContent?.trim();
  if (!raw) {
    return { kind: "landing" };
  }
  const parsed: unknown = JSON.parse(raw);
  if (
    typeof parsed === "object" &&
    parsed !== null &&
    "kind" in parsed &&
    (parsed.kind === "landing" || parsed.kind === "index" || parsed.kind === "run")
  ) {
    return parsed as Boot;
  }
  throw new Error("unreadable boot document");
}
