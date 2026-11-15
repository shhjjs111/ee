from parse_duration import parse_duration  # pyright: ignore[reportMissingImports]


def test_hours_minutes():
    assert parse_duration("1h30m") == 5400


def test_minutes_only():
    assert parse_duration("90m") == 5400
