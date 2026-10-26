import { Shell } from "../components/Shell";
import type { Boot } from "../types";

function when(value: string | null): string {
  return value ?? "—";
}

export function RunsIndex({ boot }: { boot: Extract<Boot, { kind: "index" }> }) {
  return (
    <Shell signIn={false}>
      <section className="page-open">
        <h1>{boot.heading}</h1>
        <p className="identity">
          <span>
            repo <code>{boot.repo_id}</code>
          </span>
          <span>
            org <code>{boot.org_id}</code>
          </span>
          <span>
            showing at most {boot.shown} stored run(s), newest first
          </span>
        </p>
        <p className="prose">{boot.lead}</p>
      </section>

      {boot.empty ? (
        <section className="empty" role="note">
          <p>{boot.empty}</p>
        </section>
      ) : (
        <div className="table-scroll">
          <table className="ledger">
            <caption className="sr-only">Stored CI runs, newest first</caption>
            <thead>
              <tr>
                <th>Run</th>
                <th>Commit</th>
                <th>Environment</th>
                <th>Source</th>
                <th>Revision</th>
                <th>Received</th>
                <th>Summary</th>
              </tr>
            </thead>
            <tbody>
              {boot.entries.map((entry) => (
                <tr key={entry.run_id}>
                  <td data-label="Run">
                    <a href={entry.href}>
                      <code>{entry.run_id.slice(0, 8)}</code>
                    </a>
                  </td>
                  <td data-label="Commit">
                    <code>{entry.commit_sha}</code>
                  </td>
                  <td data-label="Environment">{entry.environment}</td>
                  <td data-label="Source">{entry.source}</td>
                  <td data-label="Revision">
                    <span className="status">{entry.revision_status}</span>
                  </td>
                  <td data-label="Received">{when(entry.received_at)}</td>
                  <td data-label="Summary">
                    {entry.summary.passed}/{entry.summary.tests} passed ·{" "}
                    {entry.summary.failed} failed · {entry.summary.errored} errored ·{" "}
                    {entry.summary.skipped} skipped
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="footnote">{boot.footnote}</p>
    </Shell>
  );
}
