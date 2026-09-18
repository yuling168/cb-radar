"""Canonical CB valuation price access.

Raw prices are never overwritten; callers use these SQL expressions whenever
they need a valuation/strategy price.
"""

def effective_cb_price_sql(prefix: str = "") -> str:
    """Return the canonical effective-price SQL expression.

    ``prefix`` is an optional table alias prefix (for example ``"daily."``).
    Keeping it here prevents callers from independently reimplementing the
    close/reference fallback when their query needs qualified columns.
    """
    return f"COALESCE({prefix}close_price, {prefix}reference_price)"


def effective_cb_price_source_sql(prefix: str = "") -> str:
    """Return the canonical provenance expression for an effective price."""
    return (
        f"CASE WHEN {prefix}close_price IS NOT NULL THEN 'CLOSE' "
        f"WHEN {prefix}reference_price IS NOT NULL THEN 'REFERENCE' ELSE NULL END"
    )


# Unqualified forms retained for concise single-table strategy queries.
EFFECTIVE_CB_PRICE_SQL = effective_cb_price_sql()
EFFECTIVE_CB_PRICE_SOURCE_SQL = effective_cb_price_source_sql()


def effective_cb_price(close_price, reference_price):
    if close_price is not None:
        return close_price, "CLOSE"
    if reference_price is not None:
        return reference_price, "REFERENCE"
    return None, None
