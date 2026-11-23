<div align="center">

# ThreeWayMatchEscrow

**Procurement escrow where validators agree on facts and deterministic code decides the money: two documents, one purchase order, an exact three-way match.**

![GenLayer](https://img.shields.io/badge/GenLayer-Intelligent%20Contract-6a4cff)
![Python](https://img.shields.io/badge/Python-3.12-3776AB)
![Status](https://img.shields.io/badge/status-live%20on%20studionet-2ea44f)
![Tests](https://img.shields.io/badge/tests-13%20passed-2ea44f)
![On-chain](https://img.shields.io/badge/on--chain%20checks-26%20passed-2ea44f)

[Live Contract](https://explorer-studio.genlayer.com/address/0x5d544A28C21d0c2Aaf8d6341CA597B73Efc1Db1B) ·
[Security Audit](AUDIT.md) ·
[GenLayer Docs](https://docs.genlayer.com)

</div>

---

## Why

Accounts payable already knows the control that stops duplicate and inflated payments:
**three-way matching** — the purchase order, the delivery record and the invoice must
agree before money leaves. Doing that on-chain has always failed for one reason: someone
has to read the documents, and a contract cannot read them.

A model can read them, but letting a model *decide* a payment is exactly the thing you
must not do. This contract splits the job:

- **validators answer facts** — what reference, currency and total the invoice states, and
  whether the delivery document confirms delivery in full;
- **deterministic code decides** — the extracted values are compared against the purchase
  order terms by plain integer logic, and the escrow pays or records a mismatch.

The model never sees, and never returns, a payment decision.

## Live Deployment

| | |
|---|---|
| **Contract** | [ThreeWayMatchEscrow](https://explorer-studio.genlayer.com/address/0x5d544A28C21d0c2Aaf8d6341CA597B73Efc1Db1B) |
| **Address** | `0x5d544A28C21d0c2Aaf8d6341CA597B73Efc1Db1B` |
| **Network** | GenLayer studionet (chain `61999`) |
| **Deploy tx** | `0x169cab8c…e7b2` (code) — live instance deployed by `scripts/verify_onchain.py` |
| **Constructor args** | none |
| **On-chain code** | byte-identical to `contracts/three_way_match_escrow.py` (SHA-256 `e32c80d0…5f21`) |

## Highlights

- 🧮 **Consensus on facts, not on outcomes** — validators must agree *exactly* on five
  extracted values; the payout is then computed by contract code, so there is no
  "model said pay"
- 📄 **Both documents are hash-committed and fetched by the contract** — the supplier's
  invoice and delivery note are verified against their SHA-256 before any reading happens
- ⚖️ **The match is arithmetic, not opinion** — reference on both documents, currency,
  total inside a declared rounding tolerance, delivery confirmed in full
- 🧾 **Mismatches are recorded, not fatal** — the exact failing field is named by machine
  logic (never by the model) and the supplier may correct and resubmit, bounded by
  `MAX_ATTEMPTS = 3` and the response window
- 🔁 **No stuck funds** — the buyer can cancel an untouched order, and reclaim after the
  response window; a paid order can never be reclaimed
- 🛡️ **Tight monetary surface** — one payer, one payee, one amount; the contract never
  handles a partial split or a model-chosen figure
- 🧱 **Reusable primitive** — procurement, supplier finance, invoice factoring, DAO
  treasury payouts, grant disbursement against deliverables

## How It Works

```
open_purchase_order(po_id, supplier, currency, total_minor, tolerance_minor, window)
      |  payable: the buyer escrows the payment
      |  returns terms_binding
      v
submit_documents(po_id, invoice_url + sha256, delivery_url + sha256)   [supplier only]
      |
      |  validators: fetch both documents, verify both hashes, extract five fields
      |              and agree on them EXACTLY
      v
deterministic match (contract code)
      |
      +-- all five checks pass --> status paid, escrow released to the supplier
      |
      +-- any check fails ------> outcome mismatch, reason names the failed field,
                                  status stays open, supplier may resubmit (<= 3)
                                  
cancel_purchase_order  -> buyer, only before any submission, refund
reclaim_escrow         -> buyer, after the response window, refund
```

## The Match Rule

| Check | Source | Rule |
|---|---|---|
| Invoice reference | `invoice_po_ref` | equals the purchase order id (case and spacing normalised) |
| Invoice currency | `invoice_currency` | equals the recorded currency |
| Invoice total | `invoice_total_minor` | `abs(total - po_total) <= tolerance_minor` |
| Delivery reference | `delivery_po_ref` | equals the purchase order id |
| Delivery status | `delivery_complete` | `true` — a partial or silent delivery note fails |

Every branch names the field that failed, e.g.
`the invoice total is outside the allowed tolerance`. Those strings are produced by the
contract, so an audit trail never depends on model prose.

## Consensus Design

```
judge:
  fetch the invoice  -> verify sha256 against the commitment, else empty extraction
  fetch the delivery -> verify sha256 against the commitment, else empty extraction
  exec_prompt(purchase order terms + both documents, response_format="json")
  -> {invoice_po_ref, invoice_currency, invoice_total_minor,
      delivery_po_ref, delivery_complete, notes}

validator_fn:
  require leader Return + dict + all five keys + boolean delivery flag + non-empty notes
  re-run the same judge independently
  compare NORMALISED extraction tuples EXACTLY
```

Why this shape:

- **Facts are the only thing consensus produces.** The settlement is a pure function of the
  five values, so two accepted rounds cannot pay differently.
- **Shared normalisation.** Both the validator comparison and the payment rule call the same
  `_normalize_extraction`, so `"250,000"` and `"250000"` cannot disagree, and `"usd"` and
  `"USD"` cannot disagree.
- **`notes` is audit-only.** It must be a non-empty string and is returned to the caller, but
  no later computation consumes it.
- **Acquisition failures are recorded, never paid.** An unreachable document or a hash
  mismatch yields an empty extraction, which fails every check and produces a mismatch.
- **No storage reads inside nondet.** The order terms and both URLs/hashes are snapshotted
  into plain values before consensus; the wallet transfer happens afterwards, in
  deterministic code, after all state writes.

## Methods

| Role | Method | Guard |
|---|---|---|
| Buyer | `open_purchase_order` | payable, `value > 0`; 3-letter currency; `total_minor` in range; tolerance ≤ 5%; window 60s–90d; supplier ≠ buyer, non-zero |
| Supplier | `submit_documents` | supplier-only; order open; inside the window; `attempts < 3`; distinct public http(s) URLs; 64-hex hashes |
| Buyer | `cancel_purchase_order` | buyer-only; open; **no** submissions yet |
| Buyer | `reclaim_escrow` | buyer-only; open; response window has passed |
| Any | `get_purchase_order`, `get_order_ids` | read-only |

## Security Controls

Carried over from earlier audits on this portfolio (see [`AUDIT.md`](AUDIT.md)):

- documents fetched and hash-verified contract-side; both commitments stored with a
  `docs_binding`
- the only consequential consensus values are exactly the five the payment rule consumes
- input caps before every parse, 300 KB document cap, 6500-char prompt cap
- SSRF-hardened URL validation (no credentials, IPv6 literals, localhost, `.local`,
  `.internal`, or private/link-local/metadata ranges) on submission **and** fetch
- zero-address rejection, id allowlist, bounded attempt count, bounded order count
- all state transitions written before the wallet transfer
- `terms_binding`, `docs_binding`, `match_binding` (and `close_binding` on buyer exits)
- integer arithmetic only — no float division anywhere

## Limitations

- **The escrowed amount is not derived from the documents.** `amount` is what the buyer
  escrows in wei; `total_minor` is the documentary figure. The supplier can read both
  before submitting, and should refuse an order whose escrow is smaller than the invoice.
- **Document authenticity is not proven, only bound.** The contract guarantees the payment
  was decided from the exact bytes that were committed.
- **One invoice, one delivery note per attempt.** Multi-invoice orders should be modelled as
  separate purchase orders.
- **Tolerance is a documentary allowance**, capped at 5% of the order total by contract.
- **Not a court.** A buyer who wants to refuse payment simply never funds an order; the
  supplier's protection is that funded orders settle by rule.

## Usage Sketch

```text
# 1. buyer funds the order and fixes the documentary terms
open_purchase_order(
  po_id="PO-1001",
  supplier="0xSupplier...",
  currency="USD",
  total_minor=250000,
  tolerance_minor=2500,
  response_window_seconds=2592000,
)   # value = escrowed amount, returns terms_binding

# 2. supplier commits both documents (pinned, public URLs)
submit_documents(
  po_id="PO-1001",
  invoice_url="https://raw.githubusercontent.com/org/repo/<commit>/invoice.md",
  invoice_sha256="<64 hex>",
  delivery_url="https://raw.githubusercontent.com/org/repo/<commit>/delivery.md",
  delivery_sha256="<64 hex>",
)
# -> {"outcome": "matched", "paid_amount": 1000, "extraction": {...}}

# 3. exits
cancel_purchase_order(po_id="PO-1001")   # only before any submission
reclaim_escrow(po_id="PO-1001")          # only after the response window
```

## Test

```bash
gltest tests/ -q      # 13 passed (direct mode)
```

Coverage: the full term-validation matrix, escrow funding, supplier-only and
window-bounded submission, document commitment checks (private host, identical URLs,
malformed hashes, substituted bytes, unreachable host), the matched settlement, the
recorded-mismatch path with per-field reasons, correction and retry, the attempt bound,
tolerance edges, partial delivery, buyer cancel/reclaim paths, and the validator's exact
agreement requirement (including shared normalisation and malformed payloads).

## Lint

```bash
genvm-lint check contracts/three_way_match_escrow.py
```

`check` passes: 6 methods (2 view, 4 write), 0 constructor params.

## On-Chain Verification

Deterministic studionet run (`leader_only=True`), script
[`scripts/verify_onchain.py`](scripts/verify_onchain.py) — **26/26 PASS**:

| Step | Result |
|---|---|
| Zero escrow / bad currency / buyer as supplier / tolerance above 5% | reverted (expected) |
| `open_purchase_order` funded | accepted, `status=open`, `terms_binding` stored |
| Duplicate order id | reverted (expected) |
| Stranger submits / buyer submits | reverted (expected) |
| Private host URL / identical documents / malformed hash | reverted (expected) |
| Matched submission | accepted, `outcome=matched`, `status=paid`, `paid_at` set |
| Stored extraction | `["PO-1001", "USD", "250000", "PO-1001", "true"]` |
| Second submission after payment / reclaim after payment | reverted (expected) |
| Invoice total outside tolerance | recorded mismatch, `status=open`, attempt 1 |
| Cancel or reclaim after a submission | reverted (expected) |
| Partial delivery note | recorded mismatch, attempt 2, reason names delivery |
| Fourth attempt | reverted (expected) |
| Buyer cancels an untouched order | closed, refunded |
| Submission on a cancelled order | reverted (expected) |
| `get_order_ids` / unknown order | index correct, unknown order raises |

### Full AI consensus (real validators)

Documents pinned to commit
[`685f161`](https://github.com/dhozil/three-way-match-escrow/tree/685f16128a4c045134bda813c00f695a4390aa66/evidence);
the contract fetched and hash-verified each file itself before reading it.

**Case A — a clean match releases the escrow.** Validators: 3 `agree`, `stderr` empty.

| | |
|---|---|
| Order | `PO-2001`, 250000 minor units, tolerance 2500 |
| `submit_documents` tx | `0x35e2900b43df58566f6d4b0f0eabdade4f0a90721ffa04053681e2a2b671eb6d` |
| Agreed extraction | `["PO-2001", "USD", "250000", "PO-2001", "true"]` |
| Outcome | `matched`, `status=paid`, `paid_at` set |
| `match_binding` | `eb8c9833be23365b2f06af56eb2afeded8274bf8a15694fae495308bf7615fa7` |

**Case B — an inflated invoice is refused without touching the money.** Validators:
3 `agree`, `stderr` empty.

| | |
|---|---|
| Order | `PO-2002`, 250000 minor units, tolerance 2500 |
| `submit_documents` tx | `0x04e646d7da8c53de5065505a3160f452e4a08f37a61b0ff12844f5d99e1dc2e7` |
| Agreed extraction | `["PO-2002", "USD", "290000", "PO-2002", "true"]` |
| Outcome | `mismatch` — *the invoice total is outside the allowed tolerance* |
| Escrow | untouched, `status=open`, attempt 1 of 3 |

The two runs differ only in the committed invoice bytes. Both produced a 3/3 agreement,
and the money moved in exactly one of them — because the settlement is code, not judgment.

## Repository Layout

```
contracts/three_way_match_escrow.py   the intelligent contract
tests/test_three_way_match_escrow.py  13 direct-mode tests
scripts/verify_onchain.py             26 deterministic studionet checks
evidence/                             documents used by the on-chain runs
AUDIT.md                              security review and failure matrices
SUBMISSION.md                         portal submission text
```
