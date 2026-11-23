# Submission: ThreeWayMatchEscrow

## Category
Intelligent Contracts

## Contract Name
ThreeWayMatchEscrow

## Summary
A procurement escrow settled by a deterministic three-way match. The buyer escrows the
payment and records the purchase-order terms (reference, currency, total in minor units,
rounding tolerance). The supplier commits an invoice and a delivery document by SHA-256.
GenLayer validators fetch both documents themselves, verify the committed hashes, and
extract one fixed set of facts: the purchase order reference on each document, the
currency, the invoice total, and whether delivery was completed in full. They must agree on
those values exactly. The match is then computed by contract code — reference, currency,
total inside tolerance, delivery confirmed — and the escrow pays or records a mismatch.

## What Makes This Unique
- **Consensus on facts, not on outcomes**: the model never returns a verdict, a recipient or
  an amount. It reports what the documents say; deterministic code decides the payment.
- **Exact agreement on the values the payout consumes**: no score, no percentage, no
  partial credit. Disagreement rotates consensus instead of splitting a payment.
- **Shared normalisation**: the validator comparison and the payment rule call the same
  normaliser, so `"250,000"` / `"250000"` and `"usd"` / `"USD"` cannot produce different
  outcomes.
- **Mismatches are recorded and correctable**: the failing field is named by machine logic
  (never by model prose), the order stays open, and the supplier has three attempts inside a
  bounded response window.
- **Lock safety**: the buyer can cancel before any submission and reclaim after the window,
  so no escrow can be held forever.
- **Bindings for audit**: `terms_binding`, `docs_binding`, `match_binding`, `close_binding`.

## How Consensus Is Used
`submit_documents` runs `gl.vm.run_nondet_unsafe(judge, validator_fn)`.

- `judge` fetches the invoice and the delivery document, verifies each against its committed
  SHA-256, and calls `gl.nondet.exec_prompt(...)` with the purchase-order terms plus the
  fetched **content**, `response_format="json"`. Any acquisition failure or hash mismatch
  produces an empty extraction instead of a revert, so the order remains retryable.
- `validator_fn` requires a `Return` carrying a dict with all five keys, a boolean delivery
  flag and a non-empty notes string, then re-runs the same judge and compares the normalised
  extraction tuples **exactly**.
- Storage is snapshotted into plain values before consensus; the wallet transfer happens
  afterwards in deterministic code, after all state writes.

## Technical Details
- Python GenLayer Intelligent Contract, pinned runner
  `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
- Storage: class-level `TreeMap` declarations for `PurchaseOrder` records plus an order
  index; `u256` for amounts and counters
- Bounds: 3-letter currency, `total_minor <= 10**15`, tolerance ≤ 5%, window 60s–90d,
  3 submission attempts, 200 orders, 300 KB per document, 6500 chars per document in the
  prompt
- Methods: 6 (4 write, 2 view), no constructor params
- `genvm-lint check`: passed

## Use Case
Accounts payable and supplier finance: pay an invoice only when the purchase order, the
delivery record and the invoice actually agree. Also applicable to DAO treasury payouts,
grant disbursement against deliverables, and invoice factoring.

## Security Review
Full report: `AUDIT.md`.

| ID | Severity | Issue | Outcome |
|---|---|---|---|
| H-1 | High | A model-chosen total could be steered by text inside the invoice | Avoided by design: the model returns facts only, and the decision is code |
| M-1 | Medium | Validators normalising the same document differently | Shared normaliser used by validator and payment rule |
| M-2 | Medium | Unreachable/substituted document stalling the order | Recorded mismatch, order stays retryable |
| M-3 | Medium | Buyer cancelling mid-submission | Cancel requires zero attempts |
| M-4 | Medium | Attempt counter bypass | Counter persisted in the successful path only |
| M-5 | Low | Model prose leaking into the audit trail | Reasons are built by contract code |

## Verification
- 13/13 direct-mode tests (`gltest tests/ -q`)
- Deterministic on-chain verification on studionet (`scripts/verify_onchain.py`): **26/26 PASS**
- Full AI consensus on studionet, documents pinned to a git commit:
  - matched invoice (`PO-2001`): 3/3 `agree`, empty `stderr`, `status=paid`,
    tx `0x35e2900b43df58566f6d4b0f0eabdade4f0a90721ffa04053681e2a2b671eb6d`
  - inflated invoice (`PO-2002`, 290000 vs 250000): 3/3 `agree`, `mismatch`
    "the invoice total is outside the allowed tolerance", no payment,
    tx `0x04e646d7da8c53de5065505a3160f452e4a08f37a61b0ff12844f5d99e1dc2e7`
- On-chain code byte-identical to `contracts/three_way_match_escrow.py`

## Live Deployment
Deployed on GenLayer studionet (chain `61999`):
[`0x5d544A28C21d0c2Aaf8d6341CA597B73Efc1Db1B`](https://explorer-studio.genlayer.com/address/0x5d544A28C21d0c2Aaf8d6341CA597B73Efc1Db1B)

## Source Code
See `contracts/three_way_match_escrow.py`
