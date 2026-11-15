from dedupe import dedupe  # pyright: ignore[reportMissingImports]


def test_ints():
    assert dedupe([1, 2, 1, 3]) == [1, 2, 3]


def test_unhashable():
    a = {"x": 1}
    b = {"x": 2}
    assert dedupe([a, b, a]) == [a, b]
