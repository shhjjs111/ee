from dedupe import dedupe  # pyright: ignore[reportMissingImports]


def test_nested_unhashable():
    a = {"x": [1, 2]}
    b = {"x": [1, 3]}
    assert dedupe([a, a, b]) == [a, b]


def test_empty():
    assert dedupe([]) == []


def test_equality_not_identity():
    assert dedupe([1, True, 0, False]) == [1, 0]
