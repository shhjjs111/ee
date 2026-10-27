"""HTTP 连接池的生命周期：换模型 / 会话结束时要释放旧 client。

缺陷（已用探针复现）：ChatClient 每次自建 AsyncOpenAI + httpx 连接池，但全项目
没有任何地方关闭它们——
- `Session.set_model()` 直接把 `self._client = None`，旧连接池无人 aclose；
- `_role_clients` 缓存的多个角色 client 从不关闭；
- 会话/服务结束也没有释放路径。

后果：长会话里反复换模型（或反复开新会话）会持续堆积连接池与 fd；
Windows 上表现为 `WinError 10053`（已建立的连接被中止）这类难查的偶发失败。
"""
import asyncio

import pytest
from conftest import FakeClient

from ee.config import load_config
from ee.core.session import Session


class TrackedClient:
    """记录是否被释放的假 client（ChatClient 的最小替身）。"""

    def __init__(self, spec):
        self.spec = spec
        self.closed = False

    async def aclose(self):
        self.closed = True

    async def stream(self, messages, tools, **kw):     # pragma: no cover - 不在本测试里跑回合
        if False:
            yield None


def make_session(workspace, ee_home):
    cfg = load_config(workspace)
    # 至少两个模型，才能测“换模型”
    if len(cfg.model_names()) < 2:
        pytest.skip("配置里只有一个模型")
    # 只用 client_factory（传 client= 会被当成 override，绕过工厂）
    return Session(cfg, cwd=workspace, mode="default", persist=False,
                   client_factory=TrackedClient, home=ee_home)


def test_set_model_closes_previous_client(workspace, ee_home):
    """换模型时旧的 client 必须被释放，不能只是丢掉引用。"""
    s = make_session(workspace, ee_home)
    names = s.config.model_names()
    s.set_model(names[0])
    first = s.client()                      # client 是惰性创建的：先取出来
    assert first is not None and not first.closed

    s.set_model(names[1])
    # 有事件循环时关闭排成了任务，这里都没有循环 → 由 aclose() 收尾
    asyncio.run(s.aclose())
    assert first.closed, "换模型后旧 client 没有释放（连接池泄漏）"


def test_set_model_closes_client_inside_loop(workspace, ee_home):
    """在事件循环里换模型：旧 client 应当被异步关掉。"""
    s = make_session(workspace, ee_home)
    names = s.config.model_names()

    async def go():
        s.set_model(names[0])
        first = s.client()
        s.set_model(names[1])
        await s.aclose()
        return first

    first = asyncio.run(go())
    assert first.closed


def test_session_aclose_releases_all_clients(workspace, ee_home):
    """会话结束要能一次性释放主 client 和所有角色 client。"""
    s = make_session(workspace, ee_home)
    names = s.config.model_names()
    s.set_model(names[0])
    main_client = s.client()
    # 造两个角色 client
    roles = [sp for sp in (s.role_spec(r) for r in ("title", "compact")) if sp is not None]
    role_clients = [s.role_client(sp) for sp in roles]
    assert role_clients

    asyncio.run(s.aclose())
    assert main_client.closed
    for c in role_clients:
        assert c.closed, "角色 client 没有释放"


def test_aclose_is_idempotent(workspace, ee_home):
    """重复关闭不报错（服务退出与回合结束可能都会调）。"""
    s = make_session(workspace, ee_home)
    s.set_model(s.config.model_names()[0])
    asyncio.run(s.aclose())
    asyncio.run(s.aclose())
