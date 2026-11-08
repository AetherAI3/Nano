/** Types of `nano.qsearch.job-state.v1`; wire snake_case keys are kept as-is. */
import type { NextAction, ProviderStatus } from "./wire.js";

export type JobState =
  | "ACCEPTED"
  | "SUBMITTED_UNKNOWN"
  | "QUEUED"
  | "RUNNING"
  | "COMPLETED"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED"
  | "NOT_RUN"
  | "QUEUE_EXPIRED"
  | "CANCELLATION_REQUESTED"
  | "ACCOUNTING_PENDING"
  | "ACCOUNTING_EXPIRED"
  | "SETTLED";

export type ExecutionStatus =
  | "ACCEPTED"
  | "SUBMITTED_UNKNOWN"
  | "QUEUED"
  | "RUNNING"
  | "COMPLETED"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED"
  | "NOT_RUN";

export type ExecutionOutcome = "SUCCEEDED" | "FAILED" | "CANCELLED" | "NOT_RUN" | "UNKNOWN";
export type AccountingStatus = "PENDING" | "SETTLED" | "NOT_APPLICABLE";
export type ReservationStatus = "HELD" | "UNKNOWN" | "SETTLED" | "NOT_RESERVED";

/** `sha256:` followed by 64 lowercase hex characters. */
export type RequestDigest = string;

/**
 * A non-negative JSON number no larger than 2^53-1, or `null` where the schema
 * allows it. `null` means unknown and is never `0`.
 */
export type Amount = number;

export interface JobStateV1 {
  readonly schema: "nano.qsearch.job-state.v1";
  readonly request: {
    readonly request_id: string;
    readonly request_digest: RequestDigest;
    readonly target: string | null;
  };
  readonly lineage: {
    readonly replaces_request_digest: RequestDigest | null;
    readonly replacement_request_digest: RequestDigest | null;
  };
  readonly state: JobState;
  readonly provider: {
    readonly job_id: string | null;
    readonly status: ProviderStatus;
  };
  readonly execution: {
    readonly status: ExecutionStatus;
    readonly outcome: ExecutionOutcome;
    readonly terminal: boolean;
  };
  readonly accounting: {
    readonly status: AccountingStatus;
  };
  readonly usage: {
    readonly reservation_status: ReservationStatus;
    readonly held_quantum_seconds: Amount | null;
    readonly settled_quantum_seconds: Amount | null;
  };
  readonly cancellation: {
    readonly requested: boolean;
    readonly requested_at: Amount | null;
    readonly confirmed: boolean;
    readonly confirmed_at: Amount | null;
  };
  readonly deadlines: {
    readonly queue_at: Amount;
    readonly queue_expired: boolean;
    readonly accounting_at: Amount | null;
    readonly accounting_expired: boolean;
  };
  readonly next_action: NextAction;
}
