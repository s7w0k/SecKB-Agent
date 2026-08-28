"""MinerU 真实部署冒烟脚本。

生成一段含中文标题/段落/表格的 PDF，通过对齐到官方契约的客户端 + MinerUAdapter
发起真实解析，打印解析产物结构，并做质量门禁。

后端选择：
  ``local`` —— 本地 ``mineru-api``（默认，--base-url 指向本地服务）；
  ``agent`` —— mineru.net Agent 轻量 API（免 token，IP 限频，≤10MB/≤20 页）。

用法：
    python scripts/mineru_live_smoke.py --backend local --base-url http://127.0.0.1:8000
    python scripts/mineru_live_smoke.py --backend agent
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from reportlab.pdfgen import canvas                       # noqa: E402
from reportlab.lib.pagesizes import A4                    # noqa: E402
from reportlab.pdfbase import pdfmetrics                  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont              # noqa: E402

from app.services.document_processing.parsers.mineru import MinerUParser                    # noqa: E402
from app.services.document_processing.parsers.mineru_client import (MinerUAgentClient,       # noqa: E402
                                                                    MinerUClient)
from app.services.document_processing.quality import ParseQualityEvaluator, should_block_publish  # noqa: E402
from app.services.document_processing.profile import DocumentProfiler                       # noqa: E402


def _gen_pdf(path: str) -> None:
    """生成含中文标题/段落/表格的 PDF。"""
    _register_cjk()
    c = canvas.Canvas(path, pagesize=A4)
    c.setFont("SimSun", 20)
    c.drawString(72, 800, "心理危机干预手册")
    c.setFont("SimSun", 14)
    c.drawString(72, 770, "第一章 总则")
    c.drawString(72, 740, "第一条 本办法适用于全体员工，其目的在于规范心理危机事件的处置流程。")
    c.drawString(72, 720, "第二条 值班人员在发现高风险信号后应于十分钟内完成上报。")
    c.drawString(72, 690, "步骤一 打开配置页面并输入登录凭证。")
    c.drawString(72, 670, "步骤二 填写表单并保存变更。")
    c.drawString(72, 650, "注意：请勿重复提交。")
    # 表格
    c.drawString(72, 620, "| 字段 | 值 |")
    c.drawString(72, 600, "| 名称 | 数量 |")
    c.drawString(72, 580, "| 事件A | 12 |")
    c.drawString(72, 560, "| 事件B | 7 |")
    c.save()


def _register_cjk() -> None:
    for name, path in (("SimSun", r"C:/Windows/Fonts/simsun.ttc"), ("sans", r"C:/Windows/Fonts/msyh.ttc")):
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                return
            except Exception:
                continue
    raise RuntimeError("未找到可用中文字体，请确认系统装有 simsun.ttc 或 msyh.ttc")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="local", choices=("local", "agent"))
    ap.add_argument("--base-url", default=os.environ.get("MINERU_BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--parse-method", default="auto", choices=("auto", "txt", "ocr"))
    args = ap.parse_args()

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        pdf_path = f.name
    try:
        _gen_pdf(pdf_path)
        with open(pdf_path, "rb") as f:
            data = f.read()
        print(f"[ok] 生成样本 PDF: {pdf_path} ({len(data)} bytes)")

        if args.backend == "agent":
            client = MinerUAgentClient(timeout_seconds=args.timeout)
        else:
            client = MinerUClient(args.base_url, timeout_seconds=args.timeout, max_concurrency=2,
                                  parse_method=args.parse_method)
        print(f"[ok] backend={args.backend} MinerU health={client.health()}, "
              f"fingerprint={client.parser_fingerprint}")

        doc = MinerUParser(client).parse(data, source_uri="smoke.pdf", mime_type="application/pdf")
        print(f"[ok] 解析完成: parser_name={doc.parser_name} parse_mode={doc.parse_mode.value} blocks={len(doc.blocks)}")
        for b in doc.blocks:
            print(f"  - [{b.block_type}] page={b.page_no} path={tuple(b.section_path)} text={b.text!r}")

        quality = ParseQualityEvaluator(gate_mode="observe").evaluate(doc)
        print(f"[ok] 质量门禁: verdict={quality.verdict.value} score={quality.score} reasons={quality.reasons} blocked={should_block_publish(quality)}")

        profile = DocumentProfiler().detect(doc)
        print(f"[ok] DocumentProfile={profile.value}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[fail] {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            os.remove(pdf_path)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())