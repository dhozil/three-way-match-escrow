"""Deterministic on-chain verification for ThreeWayMatchEscrow (studionet).

Covers the term validation matrix, supplier-only and window-bounded submission,
document commitment checks, the three-way match settlement, the recorded-mismatch
path with its attempt bound, and the buyer exit paths. Runs with leader_only=True
so the deterministic mechanics are exercised without waiting for validator
agreement; the full consensus rounds are listed in README.md.

Usage (from the repository root):

  python scripts/verify_onchain.py
  python scripts/verify_onchain.py --address 0xE916a0E3...

Signing key resolution:
  1. STUDIONET_PRIVATE_KEY environment variable, or
  2. .wallets/studionet.json (gitignored) with a "private_key" field.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import genlayer_py as g
from genlayer_py import accounts
from genlayer_py.types.transactions import TransactionStatus

ROOT = Path(__file__).resolve().parents[1]
NETWORK = "studionet"
DEFAULT_CONTRACT = "contracts/three_way_match_escrow.py"
DEFAULT_BASE = (
    "https://raw.githubusercontent.com/dhozil/three-way-match-escrow/main/evidence"
)

CURRENCY = "USD"
TOTAL_MINOR = 250000
TOLERANCE_MINOR = 2500
AMOUNT = 1000


def _load_private_key() -> str:
    key = os.environ.get("STUDIONET_PRIVATE_KEY", "").strip()
    if key:
        return key
    path = ROOT / ".wallets" / f"{NETWORK}.json"
    if not path.exists():
        raise SystemExit(
            f"No key found. Set STUDIONET_PRIVATE_KEY or create {path} with a "
            '"private_key" field.'
        )
    return json.loads(path.read_text(encoding="utf-8"))["private_key"]


def _fetch_sha(url: str) -> str:
    last = None
    for _ in range(5):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return hashlib.sha256(resp.read()).hexdigest()
        except Exception as exc:  # transient 502/504 from the raw host
            last = exc
            time.sleep(2)
    raise SystemExit(f"cannot fetch {url}: {last}")


class Harness:
    def __init__(self) -> None:
        self.client_acct = accounts.create_account(_load_private_key())
        self.client = g.create_client(chain=g.studionet, account=self.client_acct)
        self.supplier = accounts.create_account()
        self.stranger = accounts.create_account()
        self.address = ""
        self.passed = 0
        self.failed = 0

    # -- plumbing -------------------------------------------------------------

    def _order(self, fn: str) -> list[str]:
        if not hasattr(self, "_schema"):
            self._schema = self.client.get_contract_schema(self.address)
        return [p[0] for p in self._schema["methods"][fn]["params"]]

    def write(self, fn: str, *, account=None, value: int = 0, leader_only=True, **kw):
        args = [kw.get(p) for p in self._order(fn)]
        last = None
        for attempt in range(6):
            try:
                return self.client.write_contract(
                    address=self.address,
                    function_name=fn,
                    account=account or self.client_acct,
                    value=value,
                    leader_only=leader_only,
                    args=args,
                )
            except Exception as exc:
                last = exc
                time.sleep(3 * (attempt + 1))
        raise SystemExit(f"write {fn} failed: {last}")

    def wait(self, tx: str) -> dict:
        last = None
        for attempt in range(4):
            try:
                return self.client.wait_for_transaction_receipt(
                    tx, status=TransactionStatus.ACCEPTED, interval=3000, retries=120
                )
            except Exception as exc:
                last = exc
                time.sleep(4 * (attempt + 1))
        raise SystemExit(f"receipt failed: {last}")

    def read(self, fn: str, **kw):
        args = [kw.get(p) for p in self._order(fn)]
        last = None
        for _ in range(6):
            try:
                return self.client.read_contract(
                    address=self.address, function_name=fn, args=args
                )
            except Exception as exc:
                last = exc
                time.sleep(3)
        raise SystemExit(f"read {fn} failed: {last}")

    def exec_result(self, rc: dict) -> str:
        leader = ((rc.get("consensus_data") or {}).get("leader_receipt") or [{}])[0]
        return str(leader.get("execution_result", ""))

    def snapshot(self, po_id: str) -> str:
        try:
            order = self.read("get_purchase_order", po_id=po_id)
        except Exception:
            return "<missing>"
        return json.dumps(
            {
                "status": order["status"],
                "attempts": order["attempts"],
                "outcome": order["outcome"],
                "paid_at": order["paid_at"],
            },
            sort_keys=True,
        )

    # -- assertions -----------------------------------------------------------

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed += 1
            print(f"  FAIL  {label} {detail}")

    def expect_fail(self, label: str, po_id: str, fn: str, **kw) -> None:
        before = self.snapshot(po_id)
        try:
            tx = self.write(fn, **kw)
            rc = self.wait(tx)
        except SystemExit:
            raise
        except Exception:
            self.check(label, True)
            return
        after = self.snapshot(po_id)
        ok = self.exec_result(rc) == "ERROR" and before == after
        self.check(label, ok, f"(exec={self.exec_result(rc)}, changed={before != after})")

    def expect_ok(self, label: str, po_id: str, fn: str, **kw) -> dict:
        before = self.snapshot(po_id)
        tx = self.write(fn, **kw)
        rc = self.wait(tx)
        after = self.snapshot(po_id)
        ok = self.exec_result(rc) == "SUCCESS" and before != after
        self.check(label, ok, f"(exec={self.exec_result(rc)}, changed={before != after})")
        return {"tx": tx, "receipt": rc}

    # -- scenario -------------------------------------------------------------

    def run(self, base: str) -> None:
        supplier = self.supplier.address
        stranger = self.stranger.address

        print("\n[1] purchase order term validation")
        self.expect_fail("zero escrow is rejected", "PO-1", "open_purchase_order",
                         po_id="PO-1", supplier=supplier, currency=CURRENCY,
                         total_minor=TOTAL_MINOR, tolerance_minor=TOLERANCE_MINOR,
                         response_window_seconds=3600, value=0)
        self.expect_fail("bad currency is rejected", "PO-1", "open_purchase_order",
                         po_id="PO-1", supplier=supplier, currency="US",
                         total_minor=TOTAL_MINOR, tolerance_minor=TOLERANCE_MINOR,
                         response_window_seconds=3600, value=AMOUNT)
        self.expect_fail("buyer cannot be the supplier", "PO-1", "open_purchase_order",
                         po_id="PO-1", supplier=self.client_acct.address,
                         currency=CURRENCY, total_minor=TOTAL_MINOR,
                         tolerance_minor=TOLERANCE_MINOR,
                         response_window_seconds=3600, value=AMOUNT)
        self.expect_fail("tolerance above 5% is rejected", "PO-1", "open_purchase_order",
                         po_id="PO-1", supplier=supplier, currency=CURRENCY,
                         total_minor=TOTAL_MINOR, tolerance_minor=50000,
                         response_window_seconds=3600, value=AMOUNT)

        tx = self.write(
            "open_purchase_order",
            value=AMOUNT,
            po_id="PO-1",
            supplier=supplier,
            currency=CURRENCY,
            total_minor=TOTAL_MINOR,
            tolerance_minor=TOLERANCE_MINOR,
            response_window_seconds=3600,
        )
        rc = self.wait(tx)
        order = self.read("get_purchase_order", po_id="PO-1")
        self.check(
            "purchase order opens and escrows the amount",
            self.exec_result(rc) == "SUCCESS"
            and order["status"] == "open"
            and order["amount"] == AMOUNT
            and len(order["terms_binding"]) == 64,
            json.dumps(order)[:200],
        )
        self.expect_fail("duplicate id is rejected", "PO-1", "open_purchase_order",
                         po_id="PO-1", supplier=supplier, currency=CURRENCY,
                         total_minor=TOTAL_MINOR, tolerance_minor=TOLERANCE_MINOR,
                         response_window_seconds=3600, value=AMOUNT)

        print("\n[2] submission authorization and document commitments")
        invoice_url = f"{base}/invoice-matched.md"
        delivery_url = f"{base}/delivery-matched.md"
        invoice_sha = _fetch_sha(invoice_url)
        delivery_sha = _fetch_sha(delivery_url)
        print(f"  invoice sha {invoice_sha[:16]}…  delivery sha {delivery_sha[:16]}…")

        self.expect_fail(
            "stranger cannot submit", "PO-1", "submit_documents",
            account=self.stranger, po_id="PO-1",
            invoice_url=invoice_url, invoice_sha256=invoice_sha,
            delivery_url=delivery_url, delivery_sha256=delivery_sha,
        )
        self.expect_fail(
            "buyer cannot submit", "PO-1", "submit_documents",
            po_id="PO-1",
            invoice_url=invoice_url, invoice_sha256=invoice_sha,
            delivery_url=delivery_url, delivery_sha256=delivery_sha,
        )
        self.expect_fail(
            "private host is refused", "PO-1", "submit_documents",
            account=self.supplier, po_id="PO-1",
            invoice_url="https://169.254.169.254/latest/meta-data",
            invoice_sha256=invoice_sha,
            delivery_url=delivery_url, delivery_sha256=delivery_sha,
        )
        self.expect_fail(
            "identical documents are refused", "PO-1", "submit_documents",
            account=self.supplier, po_id="PO-1",
            invoice_url=invoice_url, invoice_sha256=invoice_sha,
            delivery_url=invoice_url, delivery_sha256=invoice_sha,
        )
        self.expect_fail(
            "malformed hash is refused", "PO-1", "submit_documents",
            account=self.supplier, po_id="PO-1",
            invoice_url=invoice_url, invoice_sha256="abc",
            delivery_url=delivery_url, delivery_sha256=delivery_sha,
        )

        print("\n[3] three-way match settles the escrow")
        tx = self.write(
            "submit_documents",
            account=self.supplier,
            po_id="PO-1",
            invoice_url=invoice_url,
            invoice_sha256=invoice_sha,
            delivery_url=delivery_url,
            delivery_sha256=delivery_sha,
        )
        rc = self.wait(tx)
        order = self.read("get_purchase_order", po_id="PO-1")
        self.check(
            "matched submission pays and closes",
            self.exec_result(rc) == "SUCCESS"
            and order["status"] == "paid"
            and order["outcome"] == "matched"
            and order["attempts"] == 1
            and order["paid_at"] != ""
            and len(order["match_binding"]) == 64,
            json.dumps(order)[:300],
        )
        self.check(
            "extraction is stored as agreed",
            order["extraction"] == ["PO-1001", "USD", "250000", "PO-1001", "true"],
            str(order["extraction"]),
        )
        self.expect_fail(
            "double submission is refused", "PO-1", "submit_documents",
            account=self.supplier, po_id="PO-1",
            invoice_url=invoice_url, invoice_sha256=invoice_sha,
            delivery_url=delivery_url, delivery_sha256=delivery_sha,
        )
        self.expect_fail("paid escrow cannot be reclaimed", "PO-1", "reclaim_escrow",
                         po_id="PO-1")

        print("\n[4] recorded mismatch and the attempt bound")
        tx = self.write(
            "open_purchase_order",
            value=AMOUNT,
            po_id="PO-2",
            supplier=supplier,
            currency=CURRENCY,
            total_minor=TOTAL_MINOR,
            tolerance_minor=TOLERANCE_MINOR,
            response_window_seconds=3600,
        )
        self.wait(tx)
        wrong_url = f"{base}/invoice-wrong-total.md"
        wrong_sha = _fetch_sha(wrong_url)
        partial_url = f"{base}/delivery-partial.md"
        partial_sha = _fetch_sha(partial_url)

        tx = self.write(
            "submit_documents",
            account=self.supplier,
            po_id="PO-2",
            invoice_url=wrong_url,
            invoice_sha256=wrong_sha,
            delivery_url=delivery_url,
            delivery_sha256=delivery_sha,
        )
        rc = self.wait(tx)
        order = self.read("get_purchase_order", po_id="PO-2")
        self.check(
            "invoice outside tolerance is a recorded mismatch",
            self.exec_result(rc) == "SUCCESS"
            and order["status"] == "open"
            and order["outcome"] == "mismatch"
            and order["attempts"] == 1
            and "tolerance" in order["reason"],
            json.dumps(order)[:300],
        )
        self.expect_fail(
            "buyer cannot cancel after a submission", "PO-2",
            "cancel_purchase_order", po_id="PO-2",
        )
        self.expect_fail(
            "reclaim is closed before the window ends", "PO-2",
            "reclaim_escrow", po_id="PO-2",
        )

        tx = self.write(
            "submit_documents",
            account=self.supplier,
            po_id="PO-2",
            invoice_url=invoice_url,
            invoice_sha256=invoice_sha,
            delivery_url=partial_url,
            delivery_sha256=partial_sha,
        )
        rc = self.wait(tx)
        order = self.read("get_purchase_order", po_id="PO-2")
        self.check(
            "partial delivery is a recorded mismatch",
            self.exec_result(rc) == "SUCCESS"
            and order["outcome"] == "mismatch"
            and order["attempts"] == 2
            and "delivery in full" in order["reason"],
            json.dumps(order)[:300],
        )

        tx = self.write(
            "submit_documents",
            account=self.supplier,
            po_id="PO-2",
            invoice_url=wrong_url,
            invoice_sha256=wrong_sha,
            delivery_url=partial_url,
            delivery_sha256=partial_sha,
        )
        self.wait(tx)
        order = self.read("get_purchase_order", po_id="PO-2")
        self.check("third attempt is recorded", order["attempts"] == 3, json.dumps(order)[:200])
        self.expect_fail(
            "fourth attempt is refused", "PO-2", "submit_documents",
            account=self.supplier, po_id="PO-2",
            invoice_url=invoice_url, invoice_sha256=invoice_sha,
            delivery_url=delivery_url, delivery_sha256=delivery_sha,
        )

        print("\n[5] buyer exit paths")
        tx = self.write(
            "open_purchase_order",
            value=AMOUNT,
            po_id="PO-3",
            supplier=supplier,
            currency=CURRENCY,
            total_minor=TOTAL_MINOR,
            tolerance_minor=TOLERANCE_MINOR,
            response_window_seconds=60,
        )
        self.wait(tx)
        self.expect_fail("stranger cannot cancel", "PO-3", "cancel_purchase_order",
                         account=self.stranger, po_id="PO-3")
        tx = self.write("cancel_purchase_order", po_id="PO-3")
        rc = self.wait(tx)
        order = self.read("get_purchase_order", po_id="PO-3")
        self.check(
            "buyer cancels an untouched order and is refunded",
            self.exec_result(rc) == "SUCCESS"
            and order["status"] == "closed"
            and order["outcome"] == "cancelled",
            json.dumps(order)[:200],
        )
        self.expect_fail(
            "cancelled order cannot be submitted", "PO-3", "submit_documents",
            account=self.supplier, po_id="PO-3",
            invoice_url=invoice_url, invoice_sha256=invoice_sha,
            delivery_url=delivery_url, delivery_sha256=delivery_sha,
        )

        print("\n[6] views")
        ids = self.read("get_order_ids")
        self.check("order index lists every purchase order",
                   ids == ["PO-1", "PO-2", "PO-3"], str(ids))
        self.check("unknown order is not readable", self._unknown_raises(), "")


    def _unknown_raises(self) -> bool:
        try:
            self.read("get_purchase_order", po_id="missing")
        except Exception:
            return True
        return False


def deploy(h: Harness, contract: Path) -> str:
    code = contract.read_text(encoding="utf-8")
    tx = h.client.deploy_contract(code=code, account=h.client_acct)
    receipt = h.wait(tx)
    return receipt["to_address"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", default="")
    parser.add_argument("--contract", default=DEFAULT_CONTRACT)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    args = parser.parse_args()

    h = Harness()
    if args.address:
        h.address = args.address
        print(f"Using contract {h.address}")
    else:
        h.address = deploy(h, ROOT / args.contract)
        print(f"Deployed contract {h.address}")

    h.run(args.base_url.rstrip("/"))
    print(f"\n{h.passed} passed, {h.failed} failed")
    return 0 if h.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
