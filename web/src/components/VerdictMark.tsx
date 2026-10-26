import type { Verdict } from "../types";

export function VerdictMark({
  verdict,
  label,
}: {
  verdict: Verdict | null;
  label: string;
}) {
  if (verdict === null) {
    return <p className="verdict verdict-unavailable">{label}</p>;
  }
  return (
    <div className={`verdict verdict-${verdict.failure_class}`}>
      <p className="verdict-name">
        {verdict.failure_class}
        {verdict.reason ? <span className="quiet"> ({verdict.reason})</span> : null}
      </p>
      <p className="verdict-meta">
        rule {verdict.rule_version}
        {verdict.target_sha ? ` · target_sha ${verdict.target_sha}` : ""}
      </p>
      {verdict.evidence.length > 0 ? (
        <dl className="evidence">
          {verdict.evidence.map((item) => (
            <div key={item.key}>
              <dt>{item.key}</dt>
              <dd>{item.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  );
}
