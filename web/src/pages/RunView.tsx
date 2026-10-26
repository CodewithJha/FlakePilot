import { AttemptChain } from "../components/AttemptChain";
import { Shell } from "../components/Shell";
import { VerdictMark } from "../components/VerdictMark";
import type { Boot, Summary } from "../types";

const FIGURES: { key: keyof Summary; label: string; tone: string }[] = [
  { key: "tests", label: "Tests", tone: "tone-ink" },
  { key: "attempts", label: "Attempts", tone: "tone-ink" },
  { key: "passed", label: "Passed (final)", tone: "tone-pass" },
  { key: "failed", label: "Failed (final)", tone: "tone-fail" },
  { key: "errored", label: "Errored (final)", tone: "tone-fail" },
  { key: "skipped", label: "Skipped", tone: "tone-quiet" },
];

function field(value: string | null): string {
  return value && value.length > 0 ? value : "—";
}

export function RunView({ boot }: { boot: Extract<Boot, { kind: "run" }> }) {
  return (
    <Shell signIn={false}>
      <section className="page-open">
        <h1>{boot.heading}</h1>
        <dl className="facts">
          <div>
            <dt>Run</dt>
            <dd>
              <code>{boot.run_id}</code>
            </dd>
          </div>
          <div>
            <dt>Org</dt>
            <dd>
              <code>{boot.org_id}</code>
            </dd>
          </div>
          <div>
            <dt>Repo</dt>
            <dd>
              <code>{boot.repo_id}</code>
            </dd>
          </div>
          <div>
            <dt>Commit</dt>
            <dd>
              <code>{boot.commit_sha}</code>
            </dd>
          </div>
          <div>
            <dt>Environment</dt>
            <dd>
              <code>{boot.environment}</code>
            </dd>
          </div>
          <div>
            <dt>Source</dt>
            <dd>
              <code>{boot.source}</code>
            </dd>
          </div>
          <div>
            <dt>Revision</dt>
            <dd>
              <code>{boot.revision_status}</code>
            </dd>
          </div>
        </dl>
        <p className="prose">{boot.disclaimer}</p>
      </section>

      <section className="ledger-figures" aria-label="Run summary">
        <h2>Summary</h2>
        <ol>
          {FIGURES.map((figure) => (
            <li key={figure.key} className={figure.tone}>
              <span className="figure">{boot.summary[figure.key]}</span>
              <span className="figure-label">{figure.label}</span>
            </li>
          ))}
        </ol>
      </section>

      <section>
        <h2>Upload</h2>
        <div className="table-scroll">
          <table className="ledger upload">
            <tbody>
              {boot.upload.map((row) => (
                <tr key={row.label}>
                  <th scope="row">{row.label}</th>
                  <td>{field(row.value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2>Tests, attempts, and classification</h2>
        <ol className="cases">
          {boot.cases.map((testCase) => (
            <li key={`${testCase.classname}::${testCase.name}`}>
              <div className="case-id">
                <p className="classname">{testCase.classname}</p>
                <p className="casename">
                  <code>{testCase.name}</code>
                </p>
                <AttemptChain attempts={testCase.attempts} />
              </div>
              <VerdictMark verdict={testCase.classification} label={testCase.label} />
            </li>
          ))}
        </ol>
        <p className="footnote">{boot.footnote}</p>
      </section>
    </Shell>
  );
}
