"""长选项缩写绕过只读白名单。

缺陷（探针已重现，13 条里 11 条被放行）：GNU getopt_long 接受**任意唯一前缀**，
`--output` 可以写成 `--out` / `--ou` / `--o`。但 permissions.py 的拦截用的是
`a.startswith("--output")` 这类方向相反的判断——它只能匹配完整写法（或更长），
写短了就绕过：

    git grep --open='touch x; true' -e a    → 执行任意命令（--open-files-in-pager）
    git log --out=/tmp/x                    → 写文件
    sort --out=/tmp/x a.txt                 → 写文件
    sort --comp=sh a.txt                    → 执行压缩程序
    rg --pr='touch x' foo                   → 执行预处理器

## 修法

统一的 `_opt_is(text, names)`：取 `--xxx` 形式的选项名（按第一个 `=` 截断），
只要它是任一危险选项的**前缀**就判危险。方向是 `dangerous.startswith(given)`，
正好和原来的 `given.startswith(dangerous)` 相反。

前缀匹配是有意的保守选择：`--o` 对 git 同时是 `--output` / `--open-files-in-pager`
的前缀，实际 git 会因歧义报错，但我们直接拒绝——宁可让用户多写几个字母。
"""
import pytest

from ee.core.permissions import PermissionPolicy


def _policy(tmp_path, mode="read-only"):
    return PermissionPolicy(mode=mode, workspace=tmp_path)


# 探针实测被放行的那批，现在必须拦
ABBREV = [
    # git：--open-files-in-pager / --output / --ext-diff / --exec-path / --config-env
    "git grep --open='touch x; true' -e a",
    "git grep --open-files-in-pager='touch x' -e a",
    "git grep --op='touch x; true' -e a",
    "git grep --o='touch x; true' -e a",
    "git log --out=/tmp/x",
    "git diff --out=/tmp/x",
    "git status --exec=/tmp/x",
    "git status --ext",
    "git status --conf=evil",
    "git --paginate log",
    "git --pag log",
    "git status --outp=/tmp/x",
    # sort：--output / --compress-program
    "sort --out=/tmp/x a.txt",
    "sort --ou=/tmp/x a.txt",
    "sort --o=/tmp/x a.txt",
    "sort --comp=sh a.txt",
    "sort --com=sh a.txt",
    "sort --compress-program=sh a.txt",
    # rg：--pre
    "rg --pr='touch x' foo",
    "rg --pre='touch x' foo",
    "rg --p='touch x' foo",
]


@pytest.mark.parametrize("cmd", ABBREV)
def test_readonly_blocks_abbreviated_long_options(tmp_path, cmd):
    """只读模式：危险长选项的任何前缀都必须拦。"""
    assert _policy(tmp_path).check("shell", {"command": cmd}).action == "deny", cmd


@pytest.mark.parametrize("cmd", ABBREV)
def test_plan_blocks_abbreviated_long_options(tmp_path, cmd):
    assert _policy(tmp_path, "plan").check("shell", {"command": cmd}).action == "deny", cmd


@pytest.mark.parametrize("cmd", ABBREV)
def test_default_mode_asks(tmp_path, cmd):
    """默认模式至少要弹确认，不能直接放行。"""
    assert _policy(tmp_path, "default").check("shell", {"command": cmd}).action == "ask", cmd


# ---------------------------------------------------------------- 不能误伤正常用法


SAFE = [
    # git 常用只读
    "git log --oneline -5",
    "git log --stat",
    "git log --graph --oneline",
    "git diff --stat",
    "git diff --cached",
    "git status --short",
    "git --no-pager log --oneline",
    "git status --porcelain",
    "git show HEAD --pretty=oneline",
    "git grep -e foo",
    "git grep -n foo",
    "git branch --all",
    "git remote -v",              # 注：--verbose 是既有的保守拒绝，与本轮前缀匹配无关
    "git stash list",
    # sort 常用
    "sort a.txt",
    "sort -n a.txt",
    "sort -r a.txt",
    "sort --numeric-sort a.txt",
    "sort --reverse a.txt",
    "sort --unique a.txt",
    # rg 常用
    "rg foo",
    "rg -n foo",
    "rg --hidden foo",
    "rg --glob '*.py' foo",
    "rg --ignore-case foo",
    # tree
    "tree",
    "tree -a",
    "tree --dirsfirst",
    # uniq
    "uniq a.txt",
]


@pytest.mark.parametrize("cmd", SAFE)
def test_normal_readonly_commands_still_allowed(tmp_path, cmd):
    p = _policy(tmp_path, "read-only")
    got = p.check("shell", {"command": cmd}).action
    assert got == "allow", f"{cmd} -> {got}"


# ---------------------------------------------------------------- 边界：前缀方向不能反过来


def test_prefix_direction_is_dangerous_starts_with_given(tmp_path):
    """`--oneline` 不该因为 `--output` 是同字母开头就被拒。

    这条钉住方向：是"危险选项以用户输入的选项名开头"，不是反过来。
    """
    p = _policy(tmp_path, "read-only")
    assert p.check("shell", {"command": "git log --oneline"}).action == "allow"
    # 而 --out 确实是 --output 的前缀 → 必须拒
    assert p.check("shell", {"command": "git log --out=x"}).action == "deny"
