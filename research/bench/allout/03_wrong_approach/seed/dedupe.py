def dedupe(items):
    # 看起来能跑，但对不可哈希元素直接崩：这是"走错路"的起点
    return list(dict.fromkeys(items))
