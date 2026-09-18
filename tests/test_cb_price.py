from cb_price import effective_cb_price


def test_effective_price_prefers_close_and_keeps_reference_distinct():
    assert effective_cb_price(101.5, 99.5) == (101.5, "CLOSE")


def test_effective_price_uses_reference_when_close_is_missing_even_with_zero_volume():
    # Volume is intentionally not an argument: valuation price never changes
    # the independently observed trading volume.
    assert effective_cb_price(None, 99.5) == (99.5, "REFERENCE")


def test_effective_price_is_missing_only_when_both_raw_fields_are_missing():
    assert effective_cb_price(None, None) == (None, None)
