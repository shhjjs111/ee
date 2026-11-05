"""仓库相对路径的规整：git 输出的路径 → 统一形态（正斜杠、无 `./` 前缀）。"""
from __future__ import annotations


def norm_rel(p: str | None) -> str:
    """把 git 输出的相对路径规整成「正斜杠 + 无 `./` 前缀」。

    **不能用 `str.lstrip("./")`。** lstrip 的参数是**字符集合**，不是前缀：
    `.env` 会被砍成 `env`、`.github/ci.yml` 变成 `github/ci.yml`。改名之后按它拼出的
    路径根本不存在，这些文件就从清单里静默消失了——而顶层点文件恰恰是最不能漏的一类
    （`.env`、`.gitignore`、`.github/`）。

    原来的三处（handoff 的迁移清单、evidence 的引用核对、prune 的上下文裁剪）都用了
    `lstrip("./")`，同一个 bug 复制了三份。
    """
    s = (p or "").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")      # 只去开头的斜杠（绝对路径转相对），不碰任何点
