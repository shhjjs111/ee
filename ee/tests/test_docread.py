"""D4-7：read 工具读 PDF / Word / Excel / PowerPoint。测试文件现场生成，不依赖外部样本。"""
import datetime

import pytest
from conftest import run

from ee.core.docread import DocError, extract, parse_pages
from ee.tools import ToolContext, ToolError, ToolState
from ee.tools.read import ReadTool


def _ctx(ws):
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws / ".out")


def make_pdf(path, pages):
    """手写一个最小 PDF：每页一行（或空页），Helvetica 字体。"""
    objs = []
    n = len(pages)
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(n))
    objs.append("<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>")
    font_id = 3 + n * 2
    for i, text in enumerate(pages):
        content = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET" if text else ""
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {4 + i * 2} 0 R >>")
        objs.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


def test_parse_pages():
    assert parse_pages(None, 80) == list(range(1, 51))
    assert parse_pages("3", 10) == [3]
    assert parse_pages("2-4,9, 3", 10) == [2, 3, 4, 9]
    assert parse_pages("8-", 10) == [8, 9, 10]
    assert parse_pages("1，5～6", 10) == [1, 5, 6]
    with pytest.raises(DocError):
        parse_pages("abc", 10)
    with pytest.raises(DocError):
        parse_pages("20-30", 10)


def test_pdf(workspace):
    make_pdf(workspace / "a.pdf", ["Hello page one", "Second page here", "", "Fourth"])
    r = run(ReadTool().run({"path": "a.pdf"}, _ctx(workspace)))
    assert "[PDF，共 4 页，显示第 1-4 页]" in r.output
    assert "=== 第 1 页 ===" in r.output and "Hello page one" in r.output and "Fourth" in r.output
    assert "第 3 页没有文字层" in r.output
    assert r.meta["doc"] == "pdf" and r.meta["doc_info"]["pages"] == 4
    r = run(ReadTool().run({"path": "a.pdf", "pages": "2"}, _ctx(workspace)))
    assert "Second page here" in r.output and "Hello" not in r.output and "显示第 2 页" in r.output
    # 转换后的文字可分段读
    r = run(ReadTool().run({"path": "a.pdf", "offset": 3, "limit": 2}, _ctx(workspace)))
    assert "用 offset=5 继续读" in r.output


def test_docx(workspace):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("项目报告", level=1)
    d.add_paragraph("第一段正文。")
    d.add_paragraph("要点一", style="List Bullet")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "名字", "分数"
    t.cell(1, 0).text, t.cell(1, 1).text = "张三", "95"
    d.add_paragraph("表格之后的段落")
    d.save(workspace / "r.docx")
    r = run(ReadTool().run({"path": "r.docx"}, _ctx(workspace)))
    out = r.output
    assert "[Word 文档，1 个表格]" in out
    assert "# 项目报告" in out and "- 要点一" in out
    assert "| 名字 | 分数 |" in out and "| 张三 | 95 |" in out
    assert out.index("张三") < out.index("表格之后的段落")      # 保持文档顺序


def test_xlsx(workspace):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "成绩"
    ws.append(["姓名", "分数", "日期"])
    ws.append(["张三", 95.0, datetime.datetime(2026, 10, 1)])
    ws.append(["李四", 88.5, None])
    ws["E5"] = "=1+1"
    ws2 = wb.create_sheet("备注")
    ws2["A1"] = "第二个表"
    wb.save(workspace / "s.xlsx")
    r = run(ReadTool().run({"path": "s.xlsx"}, _ctx(workspace)))
    out = r.output
    assert "1. 成绩" in out and "2. 备注" in out and "工作表「成绩」" in out
    assert "行 | A | B | C" in out
    assert "2 | 张三 | 95 | 2026-10-01" in out and "3 | 李四 | 88.5" in out
    r = run(ReadTool().run({"path": "s.xlsx", "sheet": "2"}, _ctx(workspace)))
    assert "第二个表" in r.output and "张三" not in r.output
    r = run(ReadTool().run({"path": "s.xlsx", "sheet": "备注"}, _ctx(workspace)))
    assert "第二个表" in r.output
    with pytest.raises(ToolError, match="没有工作表"):
        run(ReadTool().run({"path": "s.xlsx", "sheet": "不存在"}, _ctx(workspace)))


def test_pptx(workspace):
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    for i in range(3):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = f"标题 {i + 1}"
        s.placeholders[1].text = f"内容 {i + 1}"
    prs.slides[1].notes_slide.notes_text_frame.text = "讲稿备注"
    prs.save(workspace / "p.pptx")
    r = run(ReadTool().run({"path": "p.pptx"}, _ctx(workspace)))
    assert "[PowerPoint，共 3 张，显示第 1-3 张]" in r.output
    assert "=== 第 2 张幻灯片 ===" in r.output and "内容 2" in r.output and "[备注] 讲稿备注" in r.output
    r = run(ReadTool().run({"path": "p.pptx", "pages": "3"}, _ctx(workspace)))
    assert "标题 3" in r.output and "标题 1" not in r.output


def test_errors_and_cache(workspace):
    (workspace / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0junk")
    with pytest.raises(ToolError, match="旧版"):
        run(ReadTool().run({"path": "old.doc"}, _ctx(workspace)))
    (workspace / "bad.pdf").write_bytes(b"not a pdf at all")
    with pytest.raises(ToolError, match="无法解析"):
        run(ReadTool().run({"path": "bad.pdf"}, _ctx(workspace)))
    make_pdf(workspace / "c.pdf", ["v1"])
    t1, _ = extract(workspace / "c.pdf")
    assert extract(workspace / "c.pdf")[0] is t1          # 缓存命中
    import os
    make_pdf(workspace / "c.pdf", ["version two"])
    st = os.stat(workspace / "c.pdf")
    os.utime(workspace / "c.pdf", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert "version two" in extract(workspace / "c.pdf")[0]   # 文件变了重新解析


def test_files_preview(workspace):
    from ee.server import files as F
    make_pdf(workspace / "a.pdf", ["Preview text"])
    r = F.read(str(workspace), "a.pdf")
    assert r["kind"] == "text" and r["doc"] == "pdf" and "Preview text" in r["text"]
    (workspace / "bad.xlsx").write_bytes(b"nope")
    r = F.read(str(workspace), "bad.xlsx")
    assert r["kind"] == "binary" and r["reason"] == "doc_error" and "无法解析" in r["message"]
