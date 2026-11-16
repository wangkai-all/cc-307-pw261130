"""端到端浏览器检查 (Playwright) — 验证真实网页的主要流程。

运行:
    /tmp/pwvenv/bin/python tests/e2e_check.py http://127.0.0.1:8766
"""

from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766"
SHOT = "/tmp/shots"


def main() -> int:
    import os

    os.makedirs(SHOT, exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        # 「模拟中断」故意返回 409, 浏览器会记录一条资源加载错误, 属预期行为
        page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
                if msg.type == "error" and "409" not in msg.text else None)
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector(".tl-block", timeout=10000)
        print("loaded:", page.title())
        print("rows:", page.locator(".tl-row").count(), "blocks:", page.locator(".tl-block").count())
        page.screenshot(path=f"{SHOT}/01-initial.png", full_page=False)

        # 选日期: 18 日
        page.locator("#dateRow button", has_text="11-18").first.click()
        page.wait_for_timeout(500)
        print("date:", page.locator("#dateRow button.active").inner_text().strip())

        # 加入插单
        page.locator("[data-add]").first.click()
        page.wait_for_timeout(400)
        print("waiting count:", page.locator("#waitingCount").inner_text())

        # 整体优化
        page.locator("#btnRun").click()
        page.wait_for_selector("#planCard .metric-grid", timeout=15000)
        page.wait_for_timeout(400)
        print("plan metrics:", " | ".join(
            page.locator("#planCard .metric").all_inner_texts()
        ).replace("\n", " ")[:240])
        page.screenshot(path=f"{SHOT}/02-global-trial.png", full_page=False)

        # 滚动调整
        page.locator("[data-mode=rolling]").click()
        page.locator("#cascade").evaluate("el => { el.value = 3; el.dispatchEvent(new Event('input', {bubbles:true})); }")
        page.locator("#btnRun").click()
        page.wait_for_timeout(1200)
        print("rolling mode badge:", page.locator("#planCard .badge.rolling").count())

        # 冲突展示
        print("conflicts rendered:", page.locator("#conflictList .conflict").count())

        # 点击一个块查看详情
        page.locator(".tl-block").first.click()
        page.wait_for_timeout(300)
        print("detail:", page.locator("#detailBody h3").first.inner_text())
        page.screenshot(path=f"{SHOT}/03-detail.png", full_page=False)

        # 确认 (先整体优化一个试算)
        page.locator("[data-mode=global]").click()
        page.locator("#btnRun").click()
        page.wait_for_selector("#planCard .metric-grid", timeout=15000)
        page.locator("#btnConfirm").click()
        page.wait_for_selector(".modal", timeout=5000)
        page.screenshot(path=f"{SHOT}/04-confirm.png", full_page=False)
        confirm_btn = page.locator("#btnConfirmGo")
        if confirm_btn.is_enabled():
            confirm_btn.click()
            page.wait_for_timeout(1200)
            print("after confirm, seq:", page.locator("#ledgerSeq").inner_text())
        else:
            print("confirm blocked (resource version changed)")
            page.locator("[data-close]").first.click()

        # 对比视图
        page.locator(".tabs button", has_text="方案对比").click()
        page.wait_for_timeout(600)
        page.locator("#btnParallel2").click()
        page.wait_for_timeout(2500)
        print("compare picks:", page.locator(".pick").count(),
              "rows:", page.locator("#compareTable tbody tr").count())
        page.screenshot(path=f"{SHOT}/05-compare.png", full_page=False)

        # 回放
        page.locator(".tabs button", has_text="确认与回放").click()
        page.wait_for_timeout(900)
        print("replay events:", page.locator("#replayEvents li").count(),
              "range max:", page.locator("#replayRange").get_attribute("max"))
        page.screenshot(path=f"{SHOT}/06-replay.png", full_page=False)

        # 台账 & 中断恢复
        page.locator(".tabs button", has_text="事件台账").click()
        page.wait_for_timeout(400)
        page.locator("#btnInterrupt2").click()
        page.wait_for_timeout(1200)
        print("ops:", page.locator("#ledgerOps .op").count())
        print("recovery panel:", page.locator("#recoveryPanel").inner_text().replace("\n", " / ")[:220])
        page.screenshot(path=f"{SHOT}/07-ledger.png", full_page=False)

        # 提示是否出现错误
        toasts = page.locator(".toast.error").all_inner_texts()
        print("error toasts:", toasts)
        browser.close()

    if errors:
        print("BROWSER ERRORS:")
        for e in errors:
            print("  ", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
