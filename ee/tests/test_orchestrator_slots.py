"""D6-1 U3：并发名额控制器。

缺陷形态（设计阶段就能预见）：只在 create 时检查 running 数，挡不住
"先建 10 个空闲子会话，再同时 send"——运行时并发照样超限。名额必须在**发起运行时**
预留，create 与 send 走同一个控制器；预留与释放要原子，否则并发 create 会一起通过检查。

同时钉住配置边界：项目层只能关、只能调小（与 sandbox / budget 同一条规则）。
"""
import asyncio

import pytest

from ee.config import load_config
from ee.core.orchestrator_slots import SlotController, SlotLimit


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 控制器


def test_reserve_then_release():
    async def go():
        c = SlotController(max_children=2, max_total=5)
        await c.reserve("a")
        await c.reserve("b")
        assert c.active == 2
        c.release("a")
        assert c.active == 1
        await c.reserve("c")            # 释放后能再拿
        assert c.active == 2

    run(go())


def test_over_limit_raises():
    async def go():
        c = SlotController(max_children=1, max_total=5)
        await c.reserve("a")
        with pytest.raises(SlotLimit, match="上限"):
            await c.reserve("b")

    run(go())


def test_concurrent_reserve_is_atomic():
    """10 个请求同时抢 3 个名额：必须恰好成功 3 个。"""
    async def go():
        c = SlotController(max_children=3, max_total=10)
        results = await asyncio.gather(*[c.reserve(f"k{i}") for i in range(10)],
                                       return_exceptions=True)
        ok = [r for r in results if r is None]
        assert len(ok) == 3, f"并发预留放过了 {len(ok)} 个"
        assert c.active == 3

    run(go())


def test_total_children_limit():
    """总数的限制针对已存在的子会话（含 idle），不只是正在跑的。"""
    async def go():
        c = SlotController(max_children=4, max_total=3)
        for i in range(3):
            await c.reserve(f"k{i}")
        c.release("k0")                  # 让出一个运行名额，但总数已满
        with pytest.raises(SlotLimit, match="总数"):
            await c.reserve("k9")

    run(go())


def test_release_unknown_is_noop():
    """重复释放不能把 others 的名额搞乱。"""
    c = SlotController(max_children=1, max_total=2)
    c.release("nope")
    assert c.active == 0


def test_forget_frees_total_slot():
    """删除子会话后，它的总数占用要回收，否则删了也建不了新的。"""
    async def go():
        c = SlotController(max_children=1, max_total=1)
        await c.reserve("a")
        c.release("a")
        with pytest.raises(SlotLimit, match="总数"):
            await c.reserve("b")
        c.forget("a")
        await c.reserve("b")

    run(go())


# ---------------------------------------------------------------- 配置


def _proj(workspace, text: str) -> None:
    (workspace / ".git").mkdir(exist_ok=True)
    (workspace / ".ee").mkdir(exist_ok=True)
    (workspace / ".ee" / "config.toml").write_text(text, encoding="utf-8")


def _user(ee_home, text: str) -> None:
    (ee_home / "config.toml").write_text("config_version = 3\n" + text, encoding="utf-8")


def test_default_orchestrator_is_disabled():
    from ee.config import DEFAULTS
    assert DEFAULTS["orchestrator"]["enabled"] is False


def test_project_cannot_enable_orchestrator(ee_home, workspace):
    _proj(workspace, '[orchestrator]\nenabled = true\n')
    assert load_config(workspace).data["orchestrator"]["enabled"] is False


def test_project_cannot_raise_child_limit(ee_home, workspace):
    _user(ee_home, '[orchestrator]\nenabled = true\nmax_children = 4\nmax_total_children = 8\n')
    _proj(workspace, '[orchestrator]\nmax_children = 99\nmax_total_children = 99\n')
    o = load_config(workspace).data["orchestrator"]
    assert o["max_children"] == 4 and o["max_total_children"] == 8


def test_project_can_tighten_child_limit(ee_home, workspace):
    _user(ee_home, '[orchestrator]\nenabled = true\nmax_children = 4\nmax_total_children = 8\n')
    _proj(workspace, '[orchestrator]\nmax_children = 1\n')
    assert load_config(workspace).data["orchestrator"]["max_children"] == 1


def test_orchestrator_config_reaches_controller(ee_home, workspace):
    """配置值必须能构造出控制器——不是只写在 DEFAULTS 里没人读。"""
    _user(ee_home, '[orchestrator]\nenabled = true\nmax_children = 2\nmax_total_children = 5\n')
    from ee.core.orchestrator_slots import controller_from_config
    c = controller_from_config(load_config(workspace).data)
    assert c is not None and c.max_children == 2 and c.max_total == 5


def test_disabled_config_yields_no_controller(ee_home, workspace):
    from ee.core.orchestrator_slots import controller_from_config
    assert controller_from_config(load_config(workspace).data) is None
