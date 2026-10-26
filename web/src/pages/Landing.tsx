import { FlowFigure } from "../components/FlowFigure";
import { Shell } from "../components/Shell";

const STAGES = [
  ["Ingest", "Your CI uploads its own reports. Receipts stay receipt-shaped."],
  ["Collect", "Per-test history and evidence, sorted deterministically."],
  ["Hypothesize", "An opt-in assist may suggest. The rule set still decides."],
  ["Classify", "ti-classification/1 verdicts from stored evidence only."],
  ["Reproduce", "Flaky evidence is replayed only when the execution host says so."],
] as const;

const SCHEMATIC = [
  ["tests.classify.test_turnstile", "failed → passed"],
  ["tests.upload.test_roundtrip", "passed"],
  ["tests.quota.test_cap", "—"],
] as const;

export function Landing() {
  return (
    <Shell signIn>
      <section className="opener">
        <div className="opener-lead">
          <h1>
            <span className="line">Flakes,</span>
            <span className="line">
              <span className="emph">filed.</span>
            </span>
          </h1>
          <p className="lede">
            FlakePilot ingests your CI test reports, reconstructs each run’s
            attempt chains, and classifies failures with the deterministic,
            versioned rule set <code>ti-classification/1</code> — never
            executing your repository’s code inside the control plane.
          </p>
        </div>
        <FlowFigure />
      </section>

      <section className="sheet" aria-labelledby="docket-title">
        <div className="sheet-head">
          <h2 id="docket-title">What a run reads like</h2>
          <p>
            The cells below are the shape of the classified view, with the
            verdict left empty on purpose. Your uploads fill them. Nothing
            here is a sample result.
          </p>
        </div>
        <div
          className="docket"
          role="img"
          aria-label="Schematic of the classified run view with example-shaped but empty data cells"
        >
          <div className="docket-bar">
            <span>Run</span>
            <code>abc123</code>
            <span className="quiet">ti-classification/1</span>
          </div>
          <ol>
            {SCHEMATIC.map(([name, chain]) => (
              <li key={name}>
                <code>{name}</code>
                <span className="chain-sample">{chain}</span>
                <span className="verdict-hole">verdict lands here</span>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className="stages" aria-labelledby="stages-title">
        <h2 id="stages-title">Five stages, all of them stored.</h2>
        <ol>
          {STAGES.map(([name, why], index) => (
            <li key={name}>
              <span className="stage-n">{index + 1}</span>
              <span className="stage-name">{name}</span>
              <span className="stage-why">{why}</span>
            </li>
          ))}
        </ol>
        <p className="stage-note">
          Anything advisory — an AI assist, a research source — is labeled
          advisory and opt-in. Verdicts come from the rule set. A reproduction
          claim comes from the host’s own answer. The UI invents neither.
        </p>
      </section>

      <footer className="colophon">
        <p>Read-only control plane. Execution stays with the host, never the web app.</p>
        <p>
          Health probes: <code>/health/live</code> · <code>/health/ready</code>
        </p>
      </footer>
    </Shell>
  );
}
