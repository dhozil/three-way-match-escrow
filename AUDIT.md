# Security Audit — ThreeWayMatchEscrow

**Scope:** `contracts/three_way_match_escrow.py` (studionet
`0x5d544A28C21d0c2Aaf8d6341CA597B73Efc1Db1B`), its four write paths, the consensus round
and the buyer exit paths.
**Method:** adversarial review against the failure classes found in this portfolio's earlier
audits, direct-mode regression tests, a 26-check deterministic on-chain run, and two full
AI-consensus rounds on studionet.

---

## 1. Design controls carried in from earlier audits

| Prior finding class | Control here |
|---|---|
| Model's judgment used as the payment decision | The model returns **facts only**; a deterministic `_match_reasons` function maps them to pay/no-pay |
| Validators accepting different values that change the payout | All five consumed values must match **exactly** after shared normalisation |
| Claimant-supplied hash compared against claimant-supplied content | Both documents are fetched contract-side and hashed by the contract |
| Descriptive text reused later | `notes` is required to exist and is returned, but never read by any computation or stored decision |
| Unreachable evidence blocking settlement | Acquisition failure yields an **empty extraction**, which is a recorded mismatch: the order stays open and retryable |
| Free-form numeric input | Totals are strings of digits, normalised to `int`, capped at `10**15`; no float arithmetic anywhere |
| Submitter recycling one piece of evidence | Invoice and delivery URLs must differ; each attempt binding includes the attempt number |
| Unlimited retries draining consensus | `MAX_ATTEMPTS = 3`, plus a bounded response window |
| Funds locked when nobody acts | Buyer `cancel_purchase_order` (before any submission) and `reclaim_escrow` (after the window) |
| Anyone resolving a commitment | Submissions are supplier-only, buyer exits are buyer-only |
| SSRF / private-host fetch | Public dotted host only, on submission **and** fetch |
| Pre-parse input bombs | URL/hash/id length caps, 300 KB document cap, 6500-char prompt cap |
| Transfer before state write | Every state transition is written before `emit_transfer` |
| Zero address / address confusion | Zero address rejected; supplier must differ from the buyer |
| Prohibited float division | No `/` in the contract; integer division only for the bps cap |

---

## 2. Findings from this contract's own audit

| ID | Severity | Issue | Status |
|---|---|---|---|
| H-1 | **High** | A model-chosen total could be steered by text inside the invoice ("pay 999999") | **Fixed by design** — the model never returns a decision or a target, only what the document states; the comparison is against the buyer's on-chain terms, and injected instructions are explicitly declared untrusted |
| M-1 | Medium | Two validators could normalise the same document differently (`"250,000"` vs `"250000"`, `"usd"` vs `"USD"`) and split consensus | **Fixed** — one shared `_normalize_extraction` used by both the validator and the payment rule |
| M-2 | Medium | An unreachable or substituted document could stall the order forever | **Fixed** — acquisition failure and hash mismatch become a recorded mismatch, leaving the order retryable and the window running |
| M-3 | Medium | A buyer could cancel while the supplier was mid-submission | **Fixed** — cancel requires `attempts == 0`; otherwise only the window/reclaim path is available |
| M-4 | Medium | Attempt counter could be bypassed by resubmitting in the same transaction shape | **Fixed** — the attempt is incremented and persisted in the successful path; a reverted round rolls the counter back but also moves no money |
| M-5 | Low | Mismatch reason could leak model prose into the audit trail | **Fixed** — reasons are built by contract code from the failed checks |
| L-1 | Low | Documentary figures and escrowed wei are different units | **Accepted, documented** — the supplier sees both before submitting; a mismatch of units is visible, not hidden |
| L-2 | Low | A supplier can commit a document from an unreliable host | **Accepted, documented** — the mismatch path keeps the order open and the buyer can reclaim afterwards |
| L-3 | Low | One order = one invoice, so split invoicing is not modelled | **Accepted, documented** — model each invoice as its own order |
| L-4 | Low | Tolerance is chosen by the buyer | **Accepted, documented** — capped at 5% by the contract and public before submission |

### H-1 detail

The dangerous shape would have been: *"read this invoice and tell me how much to pay"*.
That hands an attacker a prompt-injection surface with a payout attached. The implemented
shape is:

```
model  -> {reference, currency, total, delivery reference, delivered?}   (facts)
contract -> reference == PO && currency == PO && |total - PO_total| <= tolerance
            && delivered                                            (decision)
```

The model's output cannot name a recipient, an amount, or a state. Injected text can at
worst make the extraction disagree, which fails the match and pays nothing.

---

## 3. Double-submit / replay matrix

| Attempt | Result |
|---|---|
| `open_purchase_order` with an existing id | revert `Purchase order already exists` |
| `submit_documents` twice on an open order (mismatch path) | allowed up to 3 attempts, each with its own binding |
| `submit_documents` after payment | revert `Purchase order is not open` |
| `submit_documents` after cancel/reclaim | revert `Purchase order is not open` |
| 4th submission | revert `Submission attempt limit reached` |
| `cancel_purchase_order` after a submission | revert `Supplier has already submitted documents…` |
| `cancel_purchase_order` twice | revert `Purchase order is not open` |
| `reclaim_escrow` before the window | revert `Response window has not passed` |
| `reclaim_escrow` after payment | revert `Purchase order is not open` |
| Double payout for one order | impossible: `paid` is terminal and every path requires `status == open` |

---

## 4. Caller / authorization matrix

| Actor | Can | Cannot |
|---|---|---|
| Buyer (funder) | open an order; cancel before submission; reclaim after the window; reads | submit documents; force a payout; change terms or amount after opening; reclaim a paid order |
| Supplier | submit documents (≤ 3 attempts, inside the window); reads | open/cancel orders; pay itself; submit for another supplier |
| Stranger | reads | any write |
| Anyone | — | mutate the extracted facts or the recorded outcome |

`buyer` is `gl.message.sender_address` at creation; `supplier` is fixed at creation, so no
caller can act in another party's name.

---

## 5. Consensus-path audit

**Consequential-field budget.** Buyer: reference, currency, total, tolerance, escrow amount,
window. Supplier: the two document commitments. Validators: the five extracted values.
Everything else — `notes`, timestamps, prompts — is audit-only.

**Validator obligations.** `validator_fn` rejects anything that is not a `gl.vm.Return`
carrying a dict with all five keys, a boolean delivery flag and a non-empty notes string,
then re-runs `judge()` and compares the normalised tuples exactly. There is no scoring, no
partial credit and no tolerance in the comparison itself; the only tolerance in the system
is the buyer's documented `tolerance_minor`, applied by deterministic code.

**Failure handling.**

| Failure | Behaviour |
|---|---|
| Invoice unreachable / hash mismatch | empty extraction → mismatch, order stays open |
| Delivery unreachable / hash mismatch | empty extraction → mismatch, order stays open |
| Model returns an unreadable total | `_norm_minor` → `""` → mismatch naming the missing total |
| Model returns extra/injected instructions | ignored: only the five keys are read |
| Validators disagree on a value | rotation; no state change, no payment |
| Leader raises | validator returns `False`, consensus rotates |

**Prompt-injection surface.** Invoice and delivery text are delimited and labelled
untrusted, the model is told to ignore instructions inside them, and it is told never to
copy a purchase-order value into an answer unless the document prints it. The only consumed
output is five constrained values.

---

## 6. Staff feedback mapping

| Feedback theme | Verdict |
|---|---|
| Validators must verify the field that determines the outcome | **Applied** — five fields, exact agreement, re-derived from fetched documents |
| Fetch evidence contract-side and bind to what was fetched | **Applied** — both documents fetched and hash-verified before reading |
| Deterministic settlement after consensus | **Applied** — the payout rule is contract code |
| No permanent locks on inconclusive outcomes | **Applied** — recorded mismatch + attempt bound + window + reclaim |
| No premature refunds; record the adverse outcome first | **Applied** — no refund path can run while the supplier's window is open, except buyer cancel before any submission |
| Bound retries and costs | **Applied** — 3 attempts, one consensus round each |
| Bind every field reused downstream | **Applied** — model prose is never reused |
| No prohibited float division | **Compliant** |
| Verification ids and a resolvable dependency pin | **Compliant** — three bindings; pinned `py-genlayer:1jb45aa8…` |
| Storage declared as class-level storage attributes | **Applied** — first deployment failed without it, fixed and redeployed |
| Sealed commit must not receive plaintext | **N/A** — commitments here are integrity hashes over public documents |

---

## 7. Verification evidence

| Check | Result |
|---|---|
| `genvm-lint check` | pass — 6 methods (2 view, 4 write), 0 ctor params |
| Direct-mode tests | **13/13 passed** |
| Deterministic on-chain (`leader_only=True`) | **26/26 PASS** |
| Full AI consensus, matched case | 3/3 `agree`, empty `stderr`, `status=paid` (`0x35e2900b…eb6d`) |
| Full AI consensus, inflated invoice | 3/3 `agree`, empty `stderr`, `mismatch`, no payment (`0x04e646d7…c2e7`) |
| Source ↔ deployment | byte-identical (SHA-256 `e32c80d0…5f21`) |
