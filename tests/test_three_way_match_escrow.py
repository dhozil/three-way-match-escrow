"""Direct-mode tests for the ThreeWayMatchEscrow contract."""

import json
import re
from datetime import datetime, timedelta

from gltest.direct import create_address

PO_ID = "PO-1001"
BUYER_ADDR = create_address("twm-buyer")
SUPPLIER_ADDR = create_address("twm-supplier")
STRANGER_ADDR = create_address("twm-stranger")
BUYER = "0x" + BUYER_ADDR.hex()
SUPPLIER = "0x" + SUPPLIER_ADDR.hex()
STRANGER = "0x" + STRANGER_ADDR.hex()

CURRENCY = "USD"
TOTAL_MINOR = 250000
TOLERANCE_MINOR = 5000
AMOUNT = 1000
WINDOW = 3600

INVOICE_URL = "https://docs.example.com/invoice-inv-77.md"
DELIVERY_URL = "https://docs.example.com/delivery-dn-9.md"

INVOICE_OK = """# Invoice INV-77

- Purchase order: PO-1001
- Currency: USD
- Total (minor units): 250000

Payment terms: net 30.
"""

DELIVERY_OK = """# Delivery note DN-9

- Purchase order: PO-1001
- Status: delivered in full on 2024-05-01
- Received by: warehouse desk
"""

DELIVERY_PARTIAL = """# Delivery note DN-9

- Purchase order: PO-1001
- Status: partially delivered, remaining items back-ordered
"""


def _extraction(
    invoice_ref="PO-1001",
    currency="USD",
    total="250000",
    delivery_ref="PO-1001",
    delivered=True,
    notes="Read the invoice total and the delivery status from both documents.",
):
    return json.dumps(
        {
            "invoice_po_ref": invoice_ref,
            "invoice_currency": currency,
            "invoice_total_minor": total,
            "delivery_po_ref": delivery_ref,
            "delivery_complete": delivered,
            "notes": notes,
        }
    )


def _payload(**overrides):
    """The leader payload as a Python dict, which is what a validator receives."""
    return json.loads(_extraction(**overrides))


def _mock_llm(direct_vm, payload=None, total=None):
    if payload is None:
        payload = _extraction() if total is None else _extraction(total=total)
    direct_vm.mock_llm(r"reading two procurement documents", payload)


def _mock_docs(direct_vm, invoice=INVOICE_OK, delivery=DELIVERY_OK):
    direct_vm.mock_web(re.escape(INVOICE_URL), {"status": 200, "body": invoice})
    direct_vm.mock_web(re.escape(DELIVERY_URL), {"status": 200, "body": delivery})


def _open(
    direct_vm,
    contract,
    po_id=PO_ID,
    supplier=SUPPLIER,
    currency=CURRENCY,
    total=TOTAL_MINOR,
    tolerance=TOLERANCE_MINOR,
    window=WINDOW,
    value=AMOUNT,
):
    direct_vm.sender = BUYER_ADDR
    direct_vm.value = value
    return contract.open_purchase_order(
        po_id=po_id,
        supplier=supplier,
        currency=currency,
        total_minor=total,
        tolerance_minor=tolerance,
        response_window_seconds=window,
    )


def _submit(
    direct_vm,
    contract,
    po_id=PO_ID,
    invoice_url=INVOICE_URL,
    invoice_body=INVOICE_OK,
    delivery_url=DELIVERY_URL,
    delivery_body=DELIVERY_OK,
    sender=SUPPLIER_ADDR,
    invoice_sha=None,
    delivery_sha=None,
):
    import hashlib

    direct_vm.sender = sender
    return contract.submit_documents(
        po_id=po_id,
        invoice_url=invoice_url,
        invoice_sha256=invoice_sha
        or hashlib.sha256(invoice_body.encode()).hexdigest(),
        delivery_url=delivery_url,
        delivery_sha256=delivery_sha
        or hashlib.sha256(delivery_body.encode()).hexdigest(),
    )


def _happy(direct_vm, contract, payload=None):
    _mock_docs(direct_vm)
    _mock_llm(direct_vm, payload)
    return _submit(direct_vm, contract)


def test_open_purchase_order_validates_every_term(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/three_way_match_escrow.py")
    direct_vm.check_pickling = True

    with direct_vm.expect_revert("Purchase order id must be 1-64 characters"):
        _open(direct_vm, contract, po_id="")
    with direct_vm.expect_revert(
        "Purchase order id may only contain letters, digits, dash, underscore or dot"
    ):
        _open(direct_vm, contract, po_id="PO 1#")
    with direct_vm.expect_revert("Invalid Ethereum address"):
        _open(direct_vm, contract, supplier="0x123")
    with direct_vm.expect_revert("Address must not be the zero address"):
        _open(direct_vm, contract, supplier="0x0000000000000000000000000000000000000000")
    with direct_vm.expect_revert("Buyer and supplier must be different accounts"):
        _open(direct_vm, contract, supplier=BUYER)
    with direct_vm.expect_revert("Currency must be a three letter code such as USD"):
        _open(direct_vm, contract, currency="US")
    with direct_vm.expect_revert("Purchase order total is out of range"):
        _open(direct_vm, contract, total=0)
    with direct_vm.expect_revert("Tolerance must not be negative"):
        _open(direct_vm, contract, tolerance=-1)
    with direct_vm.expect_revert("Tolerance exceeds the 5% documentary allowance"):
        _open(direct_vm, contract, tolerance=TOTAL_MINOR // 10)
    with direct_vm.expect_revert("Response window is too short"):
        _open(direct_vm, contract, window=1)
    with direct_vm.expect_revert("Response window exceeds 90 days"):
        _open(direct_vm, contract, window=90 * 24 * 3600 + 1)
    with direct_vm.expect_revert("The escrowed amount must be positive"):
        _open(direct_vm, contract, value=0)

    result = _open(direct_vm, contract)
    assert result["po_id"] == PO_ID
    assert result["status"] == "open"
    assert result["escrowed_amount"] == AMOUNT
    assert len(result["terms_binding"]) == 64

    with direct_vm.expect_revert("Purchase order already exists"):
        _open(direct_vm, contract)

    order = contract.get_purchase_order(PO_ID)
    assert order["buyer"].lower() == BUYER.lower()
    assert order["supplier"].lower() == SUPPLIER.lower()
    assert order["currency"] == "USD"
    assert order["total_minor"] == TOTAL_MINOR
    assert order["tolerance_minor"] == TOLERANCE_MINOR
    assert order["amount"] == AMOUNT
    assert order["attempts"] == 0
    assert order["outcome"] == ""
    assert order["terms_binding"] == result["terms_binding"]
    assert contract.get_order_ids() == [PO_ID]


def test_matched_submission_pays_supplier_once(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/three_way_match_escrow.py")
    _open(direct_vm, contract)

    result = _happy(direct_vm, contract)
    assert result["outcome"] == "matched"
    assert result["matched"] is True
    assert result["mismatch_reasons"] == []
    assert result["paid_amount"] == AMOUNT
    assert result["attempt"] == 1
    assert len(result["docs_binding"]) == 64
    assert len(result["match_binding"]) == 64
    assert result["extraction"] == {
        "invoice_po_ref": "PO-1001",
        "invoice_currency": "USD",
        "invoice_total_minor": "250000",
        "delivery_po_ref": "PO-1001",
        "delivery_complete": "true",
    }

    order = contract.get_purchase_order(PO_ID)
    assert order["status"] == "paid"
    assert order["outcome"] == "matched"
    assert order["attempts"] == 1
    assert order["paid_at"] != ""
    assert order["match_binding"] == result["match_binding"]
    assert len(order["invoice_sha256"]) == 64
    assert order["delivery_url"] == DELIVERY_URL

    with direct_vm.expect_revert("Purchase order is not open"):
        _submit(direct_vm, contract)
    with direct_vm.expect_revert("Purchase order is not open"):
        contract.reclaim_escrow(PO_ID)


def test_invoice_total_outside_tolerance_is_a_recorded_mismatch(
    direct_vm, direct_deploy
):
    contract = direct_deploy("contracts/three_way_match_escrow.py")
    _open(direct_vm, contract)

    _mock_docs(direct_vm)
    _mock_llm(direct_vm, _extraction(total=str(TOTAL_MINOR + TOLERANCE_MINOR + 1)))
    result = _submit(direct_vm, contract)

    assert result["matched"] is False
    assert result["outcome"] == "mismatch"
    assert result["paid_amount"] == 0
    assert result["mismatch_reasons"] == [
        "the invoice total is outside the allowed tolerance"
    ]

    order = contract.get_purchase_order(PO_ID)
    assert order["status"] == "open"
    assert order["attempts"] == 1
    assert order["outcome"] == "mismatch"
    assert "tolerance" in order["reason"]


def test_inside_tolerance_still_matches(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/three_way_match_escrow.py")
    _open(direct_vm, contract)

    _mock_docs(direct_vm)
    _mock_llm(direct_vm, _extraction(total=str(TOTAL_MINOR + TOLERANCE_MINOR)))
    result = _submit(direct_vm, contract)

    assert result["matched"] is True
    assert result["paid_amount"] == AMOUNT


def test_wrong_references_and_currency_are_named_individually(
    direct_vm, direct_deploy
):
    contract = direct_deploy("contracts/three_way_match_escrow.py")
    _open(direct_vm, contract)

    _mock_docs(direct_vm)
    _mock_llm(
        direct_vm,
        _extraction(invoice_ref="PO-9999", delivery_ref="PO-8888", currency="EUR"),
    )
    result = _submit(direct_vm, contract)

    assert result["matched"] is False
    assert result["mismatch_reasons"] == [
        "the invoice purchase order reference does not match",
        "the invoice currency does not match the purchase order",
        "the delivery purchase order reference does not match",
    ]


def test_missing_document_values_are_named_without_crashing(
    direct_vm, direct_deploy
):
    contract = direct_deploy("contracts/three_way_match_escrow.py")
    _open(direct_vm, contract)

    _mock_docs(direct_vm)
    _mock_llm(
        direct_vm,
        _extraction(
            invoice_ref="", currency="", total="", delivery_ref="", delivered=False
        ),
    )
    result = _submit(direct_vm, contract)

    assert result["matched"] is False
    assert result["mismatch_reasons"] == [
        "the invoice does not state a purchase order reference",
        "the invoice does not state a currency",
        "the invoice does not state a readable total",
        "the delivery document does not state a purchase order reference",
        "the delivery document does not confirm delivery in full",
    ]


def test_partial_delivery_blocks_payment_then_correction_matches(
    direct_vm, direct_deploy
):
    contract = direct_deploy("contracts/three_way_match_escrow.py")
    _open(direct_vm, contract)

    _mock_docs(direct_vm, delivery=DELIVERY_PARTIAL)
    _mock_llm(direct_vm, _extraction(delivered=False))
    result = _submit(direct_vm, contract, delivery_body=DELIVERY_PARTIAL)

    assert result["outcome"] == "mismatch"
    assert result["mismatch_reasons"] == [
        "the delivery document does not confirm delivery in full"
    ]
    assert contract.get_purchase_order(PO_ID)["attempts"] == 1

    # The supplier corrects the delivery document and resubmits.
    direct_vm.clear_mocks()
    result = _happy(direct_vm, contract)
    assert result["outcome"] == "matched"
    assert result["attempt"] == 2
    assert result["paid_amount"] == AMOUNT


def test_attempt_limit_is_enforced(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/three_way_match_escrow.py")
    _open(direct_vm, contract)

    for attempt in (1, 2, 3):
        _mock_docs(direct_vm)
        _mock_llm(direct_vm, _extraction(total=str(TOTAL_MINOR * 2)))
        result = _submit(direct_vm, contract)
        assert result["attempt"] == attempt
        assert result["matched"] is False

    # A fourth round is refused before any consensus work happens.
    with direct_vm.expect_revert("Submission attempt limit reached"):
        _submit(direct_vm, contract)


def test_only_the_supplier_can_submit_and_only_inside_the_window(
    direct_vm, direct_deploy
):
    contract = direct_deploy("contracts/three_way_match_escrow.py")
    _open(direct_vm, contract, window=60)
    _mock_docs(direct_vm)
    _mock_llm(direct_vm)

    with direct_vm.expect_revert("Only the supplier can submit documents"):
        _submit(direct_vm, contract, sender=STRANGER_ADDR)
    with direct_vm.expect_revert("Only the supplier can submit documents"):
        _submit(direct_vm, contract, sender=BUYER_ADDR)
    with direct_vm.expect_revert("Purchase order not found"):
        _submit(direct_vm, contract, po_id="nope")

    direct_vm.warp((datetime.now() + timedelta(seconds=120)).isoformat())
    with direct_vm.expect_revert("Response window has passed"):
        _submit(direct_vm, contract)


def test_document_commitments_are_enforced(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/three_way_match_escrow.py")
    _open(direct_vm, contract)
    _mock_docs(direct_vm)
    _mock_llm(direct_vm)

    with direct_vm.expect_revert("Document URL must use a public hostname"):
        _submit(direct_vm, contract, invoice_url="https://127.0.0.1/invoice.md")
    with direct_vm.expect_revert("Document URL must be http(s)"):
        _submit(direct_vm, contract, invoice_url="ftp://docs.example.com/invoice.md")
    with direct_vm.expect_revert("Invoice and delivery documents must be different"):
        _submit(direct_vm, contract, delivery_url=INVOICE_URL)
    with direct_vm.expect_revert("Invoice hash must be 64 hex characters"):
        _submit(direct_vm, contract, invoice_sha="abc")
    with direct_vm.expect_revert("Delivery hash must be 64 hex characters"):
        _submit(direct_vm, contract, delivery_sha="0x" + "a" * 62)

    # A substituted document (hash no longer matches the fetched bytes) is recorded
    # as a mismatch instead of paying out.
    import hashlib

    result = _submit(
        direct_vm,
        contract,
        invoice_sha=hashlib.sha256(b"a different invoice").hexdigest(),
    )
    assert result["outcome"] == "mismatch"
    assert result["paid_amount"] == 0
    assert result["extraction"]["invoice_total_minor"] == ""
    order = contract.get_purchase_order(PO_ID)
    assert order["status"] == "open"
    assert order["attempts"] == 1

    # An unreachable document is also a recorded mismatch, never a payment.
    direct_vm.clear_mocks()
    direct_vm.mock_web(re.escape(INVOICE_URL), {"status": 404, "body": "gone"})
    direct_vm.mock_web(re.escape(DELIVERY_URL), {"status": 200, "body": DELIVERY_OK})
    _mock_llm(direct_vm, _extraction())
    result = _submit(direct_vm, contract)
    assert result["outcome"] == "mismatch"
    assert result["paid_amount"] == 0


def test_cancel_and_reclaim_paths(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/three_way_match_escrow.py")
    _open(direct_vm, contract, po_id="cancel-me", window=60)

    direct_vm.sender = STRANGER_ADDR
    with direct_vm.expect_revert("Only the buyer can cancel this purchase order"):
        contract.cancel_purchase_order("cancel-me")

    direct_vm.sender = BUYER_ADDR
    result = contract.cancel_purchase_order("cancel-me")
    assert result["status"] == "closed"
    assert result["refunded_amount"] == AMOUNT
    assert len(result["close_binding"]) == 64

    # Cancelling after a submission is refused: the supplier's work is not reverted
    # by the buyer, the response window is the documented exit.
    _open(direct_vm, contract, po_id="submit-then-cancel", window=60)
    _mock_docs(direct_vm)
    _mock_llm(direct_vm, _extraction(total=str(TOTAL_MINOR * 2)))
    _submit(direct_vm, contract, po_id="submit-then-cancel")

    direct_vm.sender = BUYER_ADDR
    with direct_vm.expect_revert(
        "Supplier has already submitted documents; wait for the response window"
    ):
        contract.cancel_purchase_order("submit-then-cancel")

    # Reclaim only opens after the response window closes.
    with direct_vm.expect_revert("Response window has not passed"):
        contract.reclaim_escrow("submit-then-cancel")

    direct_vm.warp((datetime.now() + timedelta(seconds=120)).isoformat())
    direct_vm.sender = STRANGER_ADDR
    with direct_vm.expect_revert("Only the buyer can reclaim this escrow"):
        contract.reclaim_escrow("submit-then-cancel")

    direct_vm.sender = BUYER_ADDR
    result = contract.reclaim_escrow("submit-then-cancel")
    assert result["status"] == "closed"
    assert result["refunded_amount"] == AMOUNT
    with direct_vm.expect_revert("Purchase order is not open"):
        contract.reclaim_escrow("submit-then-cancel")


def test_validator_requires_exact_extraction_values(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/three_way_match_escrow.py")
    _open(direct_vm, contract)
    _happy(direct_vm, contract)
    assert contract.get_purchase_order(PO_ID)["status"] == "paid"

    # A second order keeps the consensus path available for validator probes.
    _open(direct_vm, contract, po_id="PO-2002", total=TOTAL_MINOR + 100)
    direct_vm.clear_mocks()
    _mock_docs(direct_vm)
    _mock_llm(direct_vm, _extraction())
    _submit(direct_vm, contract, po_id="PO-2002")

    # Same extraction -> accept. The descriptive notes field is not compared.
    assert (
        direct_vm.run_validator(leader_result=_payload(notes="different words")) is True
    )

    # A single different consequential value -> reject.
    assert direct_vm.run_validator(leader_result=_payload(total="250001")) is False
    assert direct_vm.run_validator(leader_result=_payload(currency="EUR")) is False
    assert direct_vm.run_validator(leader_result=_payload(delivery_ref="PO-9")) is False
    assert direct_vm.run_validator(leader_result=_payload(delivered=False)) is False

    # Normalisation is shared by both sides: formatting noise is not a difference.
    assert direct_vm.run_validator(leader_result=_payload(total="250,000")) is True
    assert direct_vm.run_validator(leader_result=_payload(currency="usd")) is True

    # Malformed leader payloads are always rejected.
    assert direct_vm.run_validator(leader_result={"invoice_po_ref": "PO-1001"}) is False
    bad_bool = _payload()
    bad_bool["delivery_complete"] = "yes"
    assert direct_vm.run_validator(leader_result=bad_bool) is False
    assert direct_vm.run_validator(leader_result=_payload(notes="")) is False
    assert direct_vm.run_validator(leader_result="not a dict") is False


def test_automatic_retry_when_leader_extraction_disagrees(
    direct_vm, direct_deploy
):
    """A leader whose extraction disagrees must be rejected by the validator, so a
    wrong total can never reach the payment rule."""
    contract = direct_deploy("contracts/three_way_match_escrow.py")
    _open(direct_vm, contract)
    _mock_docs(direct_vm)
    _mock_llm(direct_vm, _extraction(total="1"))
    result = _submit(direct_vm, contract)
    assert direct_vm.run_validator(leader_result=_payload(total="250000")) is False
    assert result["outcome"] == "mismatch"
    assert contract.get_purchase_order(PO_ID)["attempts"] == 1
