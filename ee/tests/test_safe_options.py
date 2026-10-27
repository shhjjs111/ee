"""只读白名单按命令做**参数白名单**：未知选项一律不放行。

## 为什么不再用"危险选项黑名单"

前三轮（56b3c44 → 0495fd3 → d2b9aff → 9872374）都是"发现一种绕过 → 往黑名单加一条"：
成员调用裸词、参数冒号、动态表达式、GNU 长选项缩写。每修一种就冒下一种，
因为黑名单要求**穷举所有危险写法**，而 shell + PowerShell 的组合空间是开放的。

参数白名单把方向反过来：**只放行明确认识的选项**，不认识的一律拦（只读模式）
或要求确认（默认模式）。新增绕过需要攻击者只能用白名单里的选项，而白名单里
没有能执行程序/写文件的选项。

## 设计

- `SAFE_OPTS[cmd]`：该命令允许的选项集合（含短选项与长选项，长选项按 GNU 前缀缩写匹配）。
- `SAFE_OPTS[cmd]` 缺省 = 该命令**不接受任何选项**（只放行裸参数）。
- 已经按命令单独检查过的（git / gh / rg / sort / tree / uniq / sed / find / PS cmdlet）
  保留原逻辑，白名单是它的**前置门**：先过白名单，再过原有专项检查。
- 位置参数（文件名、模式串等）不受影响。

## 兼容性

只读模式原本能跑的常规命令必须继续能跑（下面 NORMAL 那批是回归网）。
新增选项从配置层面无法放开——这是有意的：需要更多选项就改代码并写测试。
"""
import pytest

from ee.core.permissions import PermissionPolicy


def _policy(tmp_path, mode="read-only", shell=None):
    return PermissionPolicy(mode=mode, workspace=tmp_path, shell=shell)


# ---------------------------------------------------------------- 未知选项必须拦


UNKNOWN_OPTS = [
    # 常见命令挂上危险/未知选项
    "ls --author",
    "ls -Z",
    "cat --show-control-chars",
    "head --frobnicate a.txt",
    "tail --no-such-flag",
    "wc --files0-from=x",
    "echo --definitely-unknown hi",
    "grep --no-such-flag foo",
    "tree --fromfile",
    "diff --to-file=x a",
    "jq --args foo",
    "column --output-separator=,",
    "stat --printf=%n a",
    "du --threshold=1K",
    "find . -fprintf",
    "sort --compress-program=sh a",
]

# git：白名单里没有的选项（能和已有子命令组合出执行/写文件）
GIT_UNKNOWN = [
    "git log --no-such-option",
    "git status --frobnicate",
    "git log --output=/tmp/x",
    "git grep --open-files-in-pager=evil -e a",
    "git diff --ext-diff",
]


@pytest.mark.parametrize("cmd", UNKNOWN_OPTS + GIT_UNKNOWN)
def test_readonly_blocks_unknown_options(tmp_path, cmd):
    """只读模式：白名单外的选项一律拒绝。"""
    assert _policy(tmp_path).check("shell", {"command": cmd}).action == "deny", cmd


@pytest.mark.parametrize("cmd", UNKNOWN_OPTS + GIT_UNKNOWN)
def test_default_mode_asks_for_unknown_options(tmp_path, cmd):
    """默认模式：白名单外的选项要弹确认，不能静默放行。"""
    assert _policy(tmp_path, "default").check("shell", {"command": cmd}).action == "ask", cmd


# ---------------------------------------------------------------- 常规用法不能误伤


NORMAL = [
    # ls / cat / head / tail / wc
    "ls", "ls -l", "ls -la", "ls -R", "ls -a", "ls --all", "ls --color=auto", "ls -lah",
    "cat a.txt", "cat -n a.txt", "cat -A a.txt",
    "head -5 a.txt", "head -n 5 a.txt", "head --lines=5 a.txt",
    "tail -5 a.txt", "tail -f a.txt", "tail -n 20 a.txt",
    "wc -l a.txt", "wc -c a.txt", "wc -w a.txt",
    "echo hi", "echo -n hi",
    "pwd", "whoami", "hostname",
    # grep / rg
    "grep foo a.txt", "grep -n foo a.txt", "grep -rn foo .", "grep -i foo a.txt",
    "grep -E 'a|b' a.txt", "grep --color=never foo a.txt", "grep -c foo a.txt",
    "rg foo", "rg -n foo", "rg -i foo", "rg --hidden foo", "rg --glob '*.py' foo",
    "rg --ignore-case foo", "rg -A 2 foo", "rg -B 2 foo", "rg -C 2 foo",
    # 文件信息
    "stat a.txt", "file a.txt", "du -sh .", "df -h", "tree", "tree -a", "tree -L 2",
    "tree --dirsfirst", "wc -l a.txt",
    # 文本处理
    "sort a.txt", "sort -n a.txt", "sort -r a.txt", "sort -u a.txt",
    "sort --numeric-sort a.txt", "sort --reverse a.txt", "sort --unique a.txt",
    "uniq a.txt", "uniq -c a.txt",
    "cut -d, -f1 a.csv", "cut -c1-5 a.txt",
    "diff a.txt b.txt", "diff -u a.txt b.txt", "diff --unified a.txt b.txt",
    "nl a.txt", "column -t a.txt", "jq . a.json", "jq -r .name a.json",
    "md5sum a.txt", "sha256sum a.txt", "cmp a.txt b.txt",
    "basename /a/b.txt", "dirname /a/b.txt", "realpath a.txt",
    "date", "uname", "uname -a", "which python", "type ls", "true",
    # git 常用只读
    "git status", "git status --short", "git status --porcelain",
    "git log", "git log --oneline", "git log --stat", "git log --graph",
    "git log -n 5", "git log --oneline -5", "git log --all", "git log --pretty=oneline",
    "git diff", "git diff --stat", "git diff --cached",
    "git show HEAD", "git show HEAD --stat",
    "git branch", "git branch -a", "git branch --all",
    "git remote", "git remote -v",
    "git rev-parse HEAD", "git ls-files", "git describe --tags",
    "git blame a.txt", "git shortlog", "git reflog",
    "git grep -e foo", "git grep -n foo",
    "git config --get user.name", "git tag", "git tag -l",
    "git ls-tree HEAD", "git cat-file -p HEAD", "git merge-base HEAD HEAD",
    "git rev-list --count HEAD", "git show-ref", "git --no-pager log --oneline",
    "git stash list", "git worktree list",
    # find（不带执行/删除类选项）
    "find . -name '*.py'", "find . -type f", "find . -maxdepth 2",
]


@pytest.mark.parametrize("cmd", NORMAL)
def test_normal_commands_still_allowed(tmp_path, cmd):
    """只读模式：常规只读用法必须继续放行（白名单不能收得太死）。

    shell 显式给 posix：shell=None 时按三套语法取最严，`grep -E 'a|b'` 里的 | 会被
    当成 cmd 的分隔符——这是既有设计（review_tools 里有同样的用例也显式指定）。
    """
    got = _policy(tmp_path, shell="posix").check("shell", {"command": cmd}).action
    assert got == "allow", f"{cmd} -> {got}"


# ---------------------------------------------------------------- 长选项缩写仍按前缀


ABBREV_OK = [
    "ls --al",                       # --all 的前缀
    "sort --rev a.txt",              # --reverse
    "sort --uniq a.txt",             # --unique
    "git log --onel",                # --oneline
    "git status --porc",             # --porcelain
    "tail --lin=5 a.txt",            # --lines
]


@pytest.mark.parametrize("cmd", ABBREV_OK)
def test_abbreviated_whitelisted_options_allowed(tmp_path, cmd):
    """白名单内的长选项，写成唯一前缀仍然放行（GNU 语义）。"""
    got = _policy(tmp_path).check("shell", {"command": cmd}).action
    assert got == "allow", f"{cmd} -> {got}"
