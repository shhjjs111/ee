"""判据探测（allout 阶段 1）。

守三件事：
- 探测只读文件、不跑命令（副作用为零）；
- 九种项目类型各自给出正确命令，优先级是「更外层的构建系统优先」；
- 探测结果一律 `confirmed=False`：陌生仓库不能让 ee 自动执行它的脚本。
"""
from __future__ import annotations

import json
import os

import pytest

from ee.core.probe import collect_test_files_ex, is_test_file, probe


def _repo(tmp_path, files: dict[str, str]) -> "object":
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- 九种项目


def test_python_pyproject(tmp_path):
    _repo(tmp_path, {"pyproject.toml": "[tool.pytest.ini_options]\ntestpaths=['tests']\n"})
    r = probe(tmp_path)
    assert r.command == "python -m pytest -q"
    assert r.source == "pyproject.toml"
    assert r.confirmed is False, "探测到的判据必须先经用户确认"


def test_node_npm_and_package_managers(tmp_path):
    npm = tmp_path / "npm"
    npm.mkdir()
    (npm / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8")
    assert probe(npm).command == "npm test"

    pnpm = tmp_path / "pnpm"
    pnpm.mkdir()
    (pnpm / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8")
    (pnpm / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    assert probe(pnpm).command == "pnpm test"

    yarn = tmp_path / "yarn"
    yarn.mkdir()
    (yarn / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8")
    (yarn / "yarn.lock").write_text("", encoding="utf-8")
    assert probe(yarn).command == "yarn test"


def test_node_placeholder_test_script_ignored(tmp_path):
    _repo(tmp_path, {"package.json": json.dumps({"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}})})
    assert probe(tmp_path).command is None


def test_cargo(tmp_path):
    _repo(tmp_path, {"Cargo.toml": "[package]\nname='x'\n"})
    assert probe(tmp_path).command == "cargo test"


def test_go(tmp_path):
    _repo(tmp_path, {"go.mod": "module example.com/x\n"})
    assert probe(tmp_path).command == "go test ./..."


def test_maven(tmp_path):
    _repo(tmp_path, {"pom.xml": "<project/>"})
    assert probe(tmp_path).command == "mvn -q test"


def test_gradle_with_counting_hint(tmp_path):
    _repo(tmp_path, {"build.gradle": "plugins { id 'java' }\n"})
    r = probe(tmp_path)
    assert r.command == "./gradlew test"
    assert "testLogging" in r.note, "gradle 默认不打印计数，必须提示"


def test_dotnet(tmp_path):
    _repo(tmp_path, {"App.csproj": "<Project/>"})
    assert probe(tmp_path).command == "dotnet test"


def test_ruby(tmp_path):
    _repo(tmp_path, {"Gemfile": "source 'https://rubygems.org'\n", "spec/a_spec.rb": ""})
    assert probe(tmp_path).command == "bundle exec rspec"


def test_php_composer_phpunit(tmp_path):
    _repo(tmp_path, {"composer.json": json.dumps({"require-dev": {"phpunit/phpunit": "^11"}})})
    assert probe(tmp_path).command == "vendor/bin/phpunit"


# ---------------------------------------------------------------- 降级与优先级


def test_nothing_detected_degrades_with_explanation(tmp_path):
    r = probe(tmp_path)
    assert r.command is None
    assert "目标" in r.note, "降级要告诉用户去哪里手填"


def test_nested_single_project_is_detected(tmp_path):
    sub = tmp_path / "ee"
    sub.mkdir()
    (sub / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    r = probe(tmp_path)
    assert r.source == "ee/pyproject.toml"
    assert r.command.startswith("powershell -NoProfile -EncodedCommand ")
    import base64
    token = r.command.rsplit(" ", 1)[-1]
    script = base64.b64decode(token).decode("utf-16-le")
    assert "Set-Location -LiteralPath 'ee'" in script
    assert "python -m pytest -q" in script
    assert "子项目" in r.note


def test_nested_multiple_projects_ask_for_choice(tmp_path):
    (tmp_path / "ee").mkdir()
    (tmp_path / "ee" / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "desktop").mkdir()
    (tmp_path / "desktop" / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8")
    r = probe(tmp_path)
    assert r.command is None, "多个子项目不能替用户猜"
    assert "多个子项目" in r.note and "ee" in r.note and "desktop" in r.note
    assert r.note.count("powershell -NoProfile -EncodedCommand") == 2
    assert "npm test" not in r.note or "desktop/package.json" in r.note


def test_multiple_build_systems_uses_outermost_and_notes_it(tmp_path):
    _repo(tmp_path, {
        "pyproject.toml": "[tool.pytest.ini_options]\n",
        "package.json": json.dumps({"scripts": {"test": "jest"}}),
    })
    r = probe(tmp_path)
    assert r.command == "python -m pytest -q", "pyproject 在探测顺序里更外层"
    assert "package.json" in r.note


def test_broken_json_does_not_crash(tmp_path):
    _repo(tmp_path, {"package.json": "{not json"})
    assert probe(tmp_path).command is None


def test_pyproject_prefers_project_venv(tmp_path):
    _repo(tmp_path, {"pyproject.toml": "[tool.pytest.ini_options]\n"})
    venv_py = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"")
    expected = (f'"{venv_py}"' if " " in str(venv_py) else str(venv_py)) + " -m pytest -q"
    assert probe(tmp_path).command == expected


# ---------------------------------------------------------------- 测试文件清单


def test_test_file_scan_does_not_follow_symlink_loop(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("", encoding="utf-8")
    try:
        os.symlink(tmp_path, tmp_path / "loop", target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不支持目录符号链接")
    files, complete = collect_test_files_ex(tmp_path)
    assert files == ["tests/test_a.py"]
    assert complete is True


def test_test_file_scan_reports_truncation(tmp_path):
    for name in ("test_a.py", "test_b.py", "test_c.py"):
        (tmp_path / name).write_text("", encoding="utf-8")
    files, complete = collect_test_files_ex(tmp_path, limit=1)
    assert len(files) == 1
    assert complete is False


def test_test_file_patterns():
    for rel in ("tests/test_a.py", "pkg/test_mod.py", "a_test.go", "src/x.test.ts",
                "src/x.spec.tsx", "__tests__/y.ts", "spec/z.rb", "tests/FooTest.php"):
        assert is_test_file(rel), rel
    for rel in ("src/app.ts", "README.md", "contest/testimony.py", "latest/notes.md", "src/Contest.php"):
        assert not is_test_file(rel), rel


def test_collect_test_files_lists_relative_paths(tmp_path):
    _repo(tmp_path, {"tests/test_a.py": "", "src/x.test.ts": "", "src/app.ts": "",
                     "node_modules/pkg/test.js": ""})
    r = probe(tmp_path)
    assert "tests/test_a.py" in r.test_files
    assert "src/x.test.ts" in r.test_files
    assert "src/app.ts" not in r.test_files
    assert not any(f.startswith("node_modules/") for f in r.test_files), "node_modules 要跳过"
