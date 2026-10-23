"""Context-independent exact arithmetic for maintenance-tier validation."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction


def exact_margin_offset(
    notional: Decimal,
    maintenance_margin_ratio: Decimal,
    cumulative_maintenance_usdt: Decimal,
) -> Fraction:
    """Return ``notional * mmr - cum`` without Decimal context rounding."""

    return (
        Fraction(notional) * Fraction(maintenance_margin_ratio)
        - Fraction(cumulative_maintenance_usdt)
    )
