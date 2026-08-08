/**
 * What the platform decided about one action.
 *
 * subhadipmitra@: `enforcing` and `suppressed` are the two properties this
 * type exists for, and the reason `#63` names them explicitly: *"the defect
 * fixed in #49 must not be reintroduced in a second client."*
 *
 * A grant in observe mode returns `allowed: true` for everything. A caller
 * that reads only `allowed` cannot tell a control that permitted an action
 * from a control that is not running — and shipping a second SDK without that
 * distinction would reintroduce, in TypeScript, the exact defect the platform
 * already fixed once.
 */

/** The raw JSON `/v1/authorize` returns. Kept separate from `Decision` so a
 *  new server field is additive rather than a type error. */
export interface DecisionPayload {
  outcome: string;
  allowed: boolean;
  reason: string;
  grant_id?: string | null;
  ledger_id?: string | null;
  remaining_amount_minor?: number | null;
  remaining_count?: number | null;
  findings?: string[] | null;
  policy_outcome?: string | null;
  enforcement_mode?: string | null;
  capability?: string | null;
  capability_expires_at?: string | null;
}

export class Decision {
  readonly outcome: string;
  readonly allowed: boolean;
  readonly reason: string;
  readonly grantId: string | null;
  readonly ledgerId: string | null;
  readonly remainingAmountMinor: number | null;
  readonly remainingCount: number | null;
  readonly findings: string[];
  /** What the policy decided, BEFORE the grant's enforcement mode was applied. */
  readonly policyOutcome: string | null;
  readonly enforcementMode: string | null;

  /**
   * The credential for THIS action, when the grant names a resource.
   *
   * subhadipmitra@: This is the difference between advice and enforcement.
   * Everything else here is something the agent may ignore — the code that
   * asks and the code that acts are the same code. A token is checked by
   * somebody else.
   *
   * Null when the grant has no audience, which is most grants.
   */
  readonly capability: string | null;
  readonly capabilityExpiresAt: string | null;

  constructor(payload: DecisionPayload) {
    this.outcome = payload.outcome;
    this.allowed = payload.allowed;
    this.reason = payload.reason;
    this.grantId = payload.grant_id ?? null;
    this.ledgerId = payload.ledger_id ?? null;
    this.remainingAmountMinor = payload.remaining_amount_minor ?? null;
    this.remainingCount = payload.remaining_count ?? null;
    this.findings = payload.findings ?? [];
    this.policyOutcome = payload.policy_outcome ?? null;
    this.enforcementMode = payload.enforcement_mode ?? null;
    this.capability = payload.capability ?? null;
    this.capabilityExpiresAt = payload.capability_expires_at ?? null;
  }

  get needsReview(): boolean {
    return this.outcome === "review_sync" || this.outcome === "review_async";
  }

  /**
   * Is this grant actually refusing anything?
   *
   * subhadipmitra@: Ask this rather than comparing `enforcementMode` to a
   * string. The set of modes will grow, and a caller who wrote
   * `mode === "enforce"` would silently start treating a new non-enforcing
   * mode as enforcement.
   *
   * Unknown — an older server that does not send the field — is reported as
   * enforcing, because assuming a control is OFF when it is on is the safer
   * error for a caller to make.
   */
  get enforcing(): boolean {
    return this.enforcementMode === null || this.enforcementMode === "enforce";
  }

  /**
   * The policy refused this and the enforcement mode let it through.
   *
   * True only while a grant is being measured. If this is ever true in
   * production, the control you believe is running is not.
   */
  get suppressed(): boolean {
    if (this.policyOutcome === null) {
      // Older servers do not send `policy_outcome`, and said the same thing in
      // the findings. Falling back rather than reporting `false` — which would
      // claim a suppression did not happen when nobody had asked.
      return this.findings.some((f) => f.startsWith("would_refuse:"));
    }
    return this.policyOutcome !== this.outcome;
  }
}
