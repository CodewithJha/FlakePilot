export interface Summary {
  tests: number;
  attempts: number;
  passed: number;
  failed: number;
  errored: number;
  skipped: number;
}

export interface IndexEntry {
  href: string;
  run_id: string;
  commit_sha: string;
  environment: string;
  source: string;
  revision_status: string;
  received_at: string | null;
  summary: Summary;
}

export interface EvidenceItem {
  key: string;
  value: string;
}

export interface Verdict {
  failure_class: string;
  reason: string | null;
  target_sha: string | null;
  rule_version: string;
  evidence: EvidenceItem[];
}

export interface RunCase {
  classname: string;
  name: string;
  attempts: string[];
  classification: Verdict | null;
  label: string;
}

export interface UploadField {
  label: string;
  value: string | null;
}

export type Boot =
  | { kind: "landing" }
  | {
      kind: "index";
      heading: string;
      org_id: string;
      repo_id: string;
      shown: number;
      lead: string;
      empty: string | null;
      footnote: string;
      entries: IndexEntry[];
    }
  | {
      kind: "run";
      heading: string;
      disclaimer: string;
      run_id: string;
      org_id: string;
      repo_id: string;
      commit_sha: string;
      environment: string;
      source: string;
      revision_status: string;
      summary: Summary;
      upload: UploadField[];
      cases: RunCase[];
      footnote: string;
    };
