"""`SessionStore.summary()` 的轻量扫描必须与 `load()` 派生的结果逐字段一致。

背景：summary 原先直接 `load()`——把每条消息、每个条目都构造成对象。thread/list
最多对几百个会话文件逐个调它，大仓库里就是几十 MB 的 JSON 解析（列表出现明显延迟）。
现在只扫 meta / 时间戳 / 用户条目的 seq 与文本，这份用例守两件事：
1. 结果与旧实现（用 load() 现算）一致，包括 reset / rewind / truncate / 老会话；
2. summary 不再调用 load（防止以后被“顺手”改回去）。
"""

from __future__ import annotations

from pathlib import Path


from ee.core.store import SessionStore


def _write(tmp: Path, sid: str, recs: list[tuple[str, dict]]) -> Path:
    st = SessionStore(tmp, sid)
    for kind, data in recs:
        st.append(kind, data)
    return st.path


def _reference(path: Path) -> dict:
    """旧实现的等价物：从 load() 结果算 summary（作为对照标准）。"""
    d = SessionStore.load(path)
    meta = d["meta"]
    return {
        "id": path.stem,
        "title": meta.get("title") or "新对话",
        "cwd": meta.get("cwd", ""),
        "model": meta.get("model", ""),
        "mode": meta.get("mode", ""),
        "created": meta.get("created"),
        "updated": meta.get("updated") or path.stat().st_mtime,
        "parent_id": meta.get("parent_id"),
        "source": meta.get("source"),
        "orchestrator_parent_id": meta.get("orchestrator_parent_id"),
        "orchestrator_role": meta.get("orchestrator_role"),
        "runs": list(meta.get("runs") or []),
        "worktree": meta.get("worktree"),
        "workspace": (meta.get("worktree") or {}).get("repo") or meta.get("cwd", ""),
    }


def _assert_same(path: Path) -> dict:
    got = SessionStore.summary(path)
    assert got == _reference(path)
    return got


def test_normal_session_matches_load(tmp_path):
    sid = "20261026-100000-aaaaaa"
    path = _write(
        tmp_path,
        sid,
        [
            (
                "meta",
                {
                    "meta": {
                        "cwd": str(tmp_path),
                        "model": "deepseek-flash",
                        "mode": "default",
                        "title": "修复登录",
                        "parent_id": "p1",
                        "orchestrator_parent_id": "o1",
                        "orchestrator_role": "child",
                    }
                },
            ),
            (
                "item",
                {
                    "item": {
                        "id": "us_1",
                        "type": "user",
                        "text": "修一下登录",
                        "seq": 1,
                    }
                },
            ),
            ("message", {"message": {"role": "user", "content": "修一下登录"}}),
            (
                "item",
                {"item": {"id": "as_1", "type": "assistant", "text": "好", "seq": 2}},
            ),
            ("message", {"message": {"role": "assistant", "content": "好"}}),
        ],
    )
    s = _assert_same(path)
    assert s["title"] == "修复登录" and s["cwd"] == str(tmp_path)
    assert s["parent_id"] == "p1" and s["orchestrator_parent_id"] == "o1"
    assert s["orchestrator_role"] == "child"
    assert s["created"] is not None and s["updated"] >= s["created"]


def test_title_after_edit_resend_uses_the_new_user_item(tmp_path):
    """编辑重发：旧的用户条目被 truncate 掉，新回合的条目 seq 更大，标题取新的那条。"""
    sid = "20261026-100001-bbbbbb"
    path = _write(
        tmp_path,
        sid,
        [
            ("meta", {"meta": {"cwd": str(tmp_path)}}),
            (
                "item",
                {
                    "item": {
                        "id": "us_1",
                        "type": "user",
                        "text": "被编辑掉的旧问题",
                        "seq": 1,
                    }
                },
            ),
            (
                "item",
                {"item": {"id": "us_5", "type": "user", "text": "中段问题", "seq": 5}},
            ),
            ("truncate", {"from_seq": 1}),
            (
                "item",
                {
                    "item": {
                        "id": "us_7",
                        "type": "user",
                        "text": "真正留下的问题",
                        "seq": 7,
                    }
                },
            ),
            (
                "item",
                {"item": {"id": "as_8", "type": "assistant", "text": "答", "seq": 8}},
            ),
        ],
    )
    s = _assert_same(path)
    assert s["title"] == "真正留下的问题"


def test_title_from_messages_for_old_session_without_items(tmp_path):
    sid = "20261026-100002-cccccc"
    path = _write(
        tmp_path,
        sid,
        [
            ("meta", {"meta": {"cwd": str(tmp_path)}}),
            ("message", {"message": {"role": "user", "content": "[系统提示] 初始化"}}),
            (
                "message",
                {
                    "message": {
                        "role": "user",
                        "content": "终端时代的第一句\n\n<context-update seq=1>…",
                    }
                },
            ),
            ("message", {"message": {"role": "assistant", "content": "收到"}}),
        ],
    )
    s = _assert_same(path)
    assert s["title"] == "终端时代的第一句"  # 去掉上下文附录，跳过 [系统提示]


def test_rewind_and_reset_match_load(tmp_path):
    sid = "20261026-100003-dddddd"
    path = _write(
        tmp_path,
        sid,
        [
            ("meta", {"meta": {"cwd": str(tmp_path), "title": "x"}}),
            ("message", {"message": {"role": "user", "content": "第一句"}}),
            ("message", {"message": {"role": "assistant", "content": "答 1"}}),
            ("rewind", {"to": 1}),
            ("message", {"message": {"role": "user", "content": "第二句"}}),
            ("reset", {}),
            ("message", {"message": {"role": "user", "content": "重置后第一句"}}),
        ],
    )
    _assert_same(path)


def test_stale_run_is_closed_in_summary(tmp_path):
    sid = "20261026-100004-eeeeee"
    path = _write(
        tmp_path,
        sid,
        [
            (
                "meta",
                {
                    "meta": {
                        "cwd": str(tmp_path),
                        "runs": [
                            {
                                "id": "run_1",
                                "status": "running",
                                "terminal_reason": None,
                                "pid": 999_999_999,
                            },
                        ],
                    }
                },
            ),
            (
                "item",
                {
                    "item": {
                        "id": "us_1",
                        "type": "user",
                        "text": "跑一半被杀",
                        "seq": 1,
                    }
                },
            ),
        ],
    )
    s = _assert_same(path)
    assert s["runs"][-1]["status"] == "done"
    assert s["runs"][-1]["terminal_reason"] == "interrupted"


def test_summary_does_not_call_load(tmp_path, monkeypatch):
    sid = "20261026-100005-ffffff"
    path = _write(
        tmp_path,
        sid,
        [
            ("meta", {"meta": {"cwd": str(tmp_path), "title": "轻量"}}),
            ("message", {"message": {"role": "user", "content": "hi"}}),
        ],
    )

    def boom(cls, p):  # noqa: ANN001
        raise AssertionError("summary() 又去全量 load() 了：thread/list 会被拖慢")

    monkeypatch.setattr(SessionStore, "load", classmethod(boom))
    assert SessionStore.summary(path)["title"] == "轻量"


def test_report_uses_summary_not_load(tmp_path, monkeypatch):
    """接线断言：thread/list 的补文件路径必须走 summary（它不是热路径上的全量解析）。"""
    import inspect

    from ee.server import app as A

    src = inspect.getsource(A.Backend._thread_list)
    assert "SessionStore.summary(p)" in src
    assert "SessionStore.load(" not in src
