import re


def slugify(text):
    """把标题转成 URL slug。规格见 task.md（本实现只覆盖了很小一部分）。"""
    return re.sub(r"\s+", "-", text.strip().lower())
