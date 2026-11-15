def chunks(items, size):
    if size <= 0:
        raise ValueError("size 必须为正数")
    return [items[i:i + size - 1] for i in range(0, len(items), size)]
