import { useState } from "react";
import { readBoot } from "./boot";
import { Shell } from "./components/Shell";
import { Landing } from "./pages/Landing";
import { RunView } from "./pages/RunView";
import { RunsIndex } from "./pages/RunsIndex";
import type { Boot } from "./types";

function load(): { boot: Boot | null; error: boolean } {
  try {
    return { boot: readBoot(), error: false };
  } catch {
    return { boot: null, error: true };
  }
}

export function App() {
  const [state] = useState(load);
  if (state.error || state.boot === null) {
    return (
      <Shell signIn={false}>
        <section className="empty">
          <h1>This page could not be read.</h1>
          <p className="prose">
            The document from this origin was incomplete. Reload the page. Nothing
            was executed.
          </p>
        </section>
      </Shell>
    );
  }
  if (state.boot.kind === "index") {
    return <RunsIndex boot={state.boot} />;
  }
  if (state.boot.kind === "run") {
    return <RunView boot={state.boot} />;
  }
  return <Landing />;
}
