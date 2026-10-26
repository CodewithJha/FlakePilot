const ATTEMPTS = [
  ["failed", "fail"],
  ["failed", "fail"],
  ["passed", "pass"],
] as const;

const FACTS = ["counts", "environments", "commit SHAs"] as const;

export function FlowFigure() {
  return (
    <figure className="flow">
      <ol className="flow-steps">
        <li className="flow-step">
          <span className="flow-label">Attempts</span>
          <div className="flow-chain">
            <span className="flow-wire" aria-hidden="true" />
            {ATTEMPTS.map(([name, tone], index) => (
              <span key={name + index} className="flow-node" data-tone={tone}>
                {name}
              </span>
            ))}
          </div>
          <p className="flow-note">The chain, as CI stored it.</p>
        </li>
        <li className="flow-step">
          <span className="flow-label">Evidence</span>
          <ul className="flow-facts">
            {FACTS.map((fact) => (
              <li key={fact}>{fact}</li>
            ))}
          </ul>
          <p className="flow-note">Recorded facts. No score is added.</p>
        </li>
        <li className="flow-step">
          <span className="flow-label">Verdict</span>
          <div className="flow-verdict">
            <p className="flow-stamp">one class</p>
            <p className="flow-note">
              <code>ti-classification/1</code>, from stored evidence only.
            </p>
          </div>
        </li>
      </ol>
      <figcaption>
        A diagram of the reading, not a result from a repository. Nothing here
        executes.
      </figcaption>
    </figure>
  );
}
