from parse_duration import parse_duration  # pyright: ignore[reportMissingImports]


def test_decimal_hours():
    assert parse_duration("1.5h") == 5400


def test_negative():
    assert parse_duration("-1h") == -3600


def test_unknown_unit_rejected():
    try:
        parse_duration("1d")
    except ValueError:
        pass
    else:
        raise AssertionError("未知单位应当抛 ValueError")
