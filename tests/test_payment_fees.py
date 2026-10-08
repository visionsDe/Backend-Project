"""Fee / net math for the SCT release step."""
from decimal import Decimal
from types import SimpleNamespace

from app.routes.payments import compute_release_amounts


def _txn(amount, refunded_amount=0, stripe_fee_amount=0):
    return SimpleNamespace(
        amount=amount,
        refunded_amount=refunded_amount,
        stripe_fee_amount=stripe_fee_amount,
    )


def test_full_amount_no_fee():
    fee, net = compute_release_amounts(_txn(100))
    assert fee == Decimal("8.00")
    assert net == Decimal("92.00")


def test_partial_refund_shrinks_fee_and_net():
    """Participation fee applies only to the non-refunded portion — a 25%
    refund should drop the fee from 8.00 to 6.00 and the net to 69.00."""
    fee, net = compute_release_amounts(_txn(100, refunded_amount=25))
    assert fee == Decimal("6.00")
    assert net == Decimal("69.00")


def test_stripe_fee_comes_off_net_only():
    """Stripe processing fee is deducted from the net paid out — it does
    not reduce the platform's participation fee."""
    fee, net = compute_release_amounts(_txn(100, stripe_fee_amount=3))
    assert fee == Decimal("8.00")
    assert net == Decimal("89.00")


def test_refund_larger_than_amount_floors_at_zero():
    fee, net = compute_release_amounts(_txn(100, refunded_amount=150))
    assert fee == Decimal("0.00")
    assert net == Decimal("0.00")


def test_none_values_treated_as_zero():
    fee, net = compute_release_amounts(_txn(100, refunded_amount=None, stripe_fee_amount=None))
    assert fee == Decimal("8.00")
    assert net == Decimal("92.00")
