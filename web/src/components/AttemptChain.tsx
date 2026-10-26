export function AttemptChain({ attempts }: { attempts: string[] }) {
  if (attempts.length === 0) {
    return <span className="quiet">no attempts stored</span>;
  }
  return (
    <ol className="chain" aria-label="Attempts, chronological">
      {attempts.map((attempt, index) => (
        <li key={`${index}-${attempt}`} data-outcome={attempt}>
          <span className="node" aria-hidden="true" />
          <span className="node-label">{attempt}</span>
        </li>
      ))}
    </ol>
  );
}
