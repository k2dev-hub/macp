#!/usr/bin/env python3
"""Real browser E2E for the MACP console (Playwright + Chromium).

Runs inside mcr.microsoft.com/playwright/python with --network host so it reaches
the deployment on macp.example.com:9443 (self-signed certificate, ignored here).

Flow (mirrors the handover checklist):
  1. read-only visitor: overview renders, no task-creation affordance, admin menus hidden
  2. token login: "配置 Token" -> identity badge appears
  3. create task through the modal
  4. select the row -> "批量派单" -> task reaches review/merged
  5. logout
  6. SSO login through the built-in OIDC provider form
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("E2E_BASE", "https://localhost:9443")
# Optional: drive an already-installed browser instead of a downloaded one,
# e.g. E2E_CHANNEL=msedge on a Windows workstation (no browser download needed).
CHANNEL = os.environ.get("E2E_CHANNEL", "").strip() or None
# Optional: path to an installed Chromium binary (server-side runs, no download).
EXECUTABLE = os.environ.get("E2E_EXECUTABLE", "").strip() or None
TOKEN_FILE = os.environ.get("E2E_TOKEN_FILE", "/run/secrets/wtok")
OIDC_PW_FILE = os.environ.get("E2E_OIDC_PW_FILE", "/run/secrets/oidcpw")
OUT = Path(os.environ.get("E2E_OUT", "/e2e/out"))
TOKEN = (
    os.environ.get("E2E_TOKEN", "").strip()
    or Path(TOKEN_FILE).read_text(encoding="utf-8").strip()
)
OIDC_PASSWORD = (
    os.environ.get("E2E_OIDC_PASSWORD", "").strip()
    or Path(OIDC_PW_FILE).read_text(encoding="utf-8").strip()
)
CODE = os.environ.get("E2E_TASK", f"T-95{int(time.time()) % 10000:04d}")

failures: list[str] = []
steps: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f" | {detail}" if detail else ""), flush=True)
    steps.append(("PASS " if ok else "FAIL ") + name)
    if not ok:
        failures.append(name)


def shot(page, name: str) -> None:
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    except Exception:
        pass


def main() -> int:
    with sync_playwright() as pw:
        # Three launch modes, in order of preference:
        #   1. E2E_EXECUTABLE: any installed Chromium (e.g. /usr/bin/chromium on .10).
        #      Debian's build traps (SIGTRAP) unless --no-zygote is passed.
        #   2. E2E_CHANNEL: a branded build already on the machine (msedge/chrome).
        #   3. Playwright's bundled browser (needs the download).
        args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        if EXECUTABLE:
            args.append("--no-zygote")
        browser = pw.chromium.launch(
            executable_path=EXECUTABLE, channel=CHANNEL, args=args
        )
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1500, "height": 950})
        page = context.new_page()
        page.set_default_timeout(20000)

        print(f"== 1. read-only visitor ==")
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=30000)
        check("page title is the console", "MACP Mission Control" in page.title(), page.title())
        body = page.inner_text("body")
        check("overview renders", "总览" in body or "Mission Control" in body, body[:80].replace("\n", " "))
        check("read-only CTA present", "登录后新建" in body)
        check("admin token button visible", "配置 Token" in body)
        shot(page, "01-readonly")

        print("== 2. token login ==")
        page.get_by_role("button", name="配置 Token").click()
        page.wait_for_selector('input[placeholder="MACP write token"]', timeout=15000)
        page.fill('input[placeholder="MACP write token"]', TOKEN)
        # AntD inserts a space between two CJK characters in button labels
        # ("保存" renders as "保 存" and autoInsertSpace can be turned off), so
        # click the modal's primary footer button instead of matching text.
        page.locator(".ant-modal:visible .ant-modal-footer .ant-btn-primary").click()
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        check("identity badge shows admin token", "已登录" in body and "Token" in body, body[:80])
        shot(page, "02-loggedin")

        print("== 3. create task via the modal ==")
        page.locator(".ant-btn-primary", has_text="新建任务").first.click()
        page.wait_for_selector('input[placeholder="T-900001"]', timeout=15000)
        page.fill('input[placeholder="T-900001"]', CODE)
        page.fill('input[placeholder="一句话说明要做什么"]', "browser e2e probe")
        page.locator(".ant-modal:visible .ant-modal-footer .ant-btn-primary").click()
        page.wait_for_timeout(2500)
        body = page.inner_text("body")
        check(f"task {CODE} appears in the table", CODE in body, body[:120])
        shot(page, "03-created")

        print("== 4. batch dispatch from the task page ==")
        # The overview lists recent tasks; row selection only exists on the
        # task page, reached from the hero button "查看任务".
        page.get_by_role("button", name="查看任务").first.click()
        page.wait_for_timeout(1500)
        shot(page, "04-taskpage")
        row = page.locator("tr", has_text=CODE).first
        row.locator("input.ant-checkbox-input").check()
        page.wait_for_timeout(500)
        page.locator("button", has_text="批量派单").first.click()
        page.wait_for_timeout(3000)
        body = page.inner_text("body")
        check("batch dispatch reported completion", "批量派单完成" in body, body[:120])
        shot(page, "04-dispatched")

        print("== 5. wait for the task to finish ==")
        deadline = time.time() + 150
        state = ""
        while time.time() < deadline:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            body = page.inner_text("body")
            for candidate in ("已合并", "待验收", "已验收", "执行中"):
                if candidate in body:
                    state = candidate
            if "已合并" in state or "已合并" in body:
                state = "已合并"
                break
        check("task reached 已合并 (merged) in the UI", "已合并" in state or "已合并" in page.inner_text("body"), state)
        shot(page, "05-merged")

        print("== 6. logout ==")
        page.get_by_role("button", name="退出登录").click()
        page.wait_for_timeout(1200)
        body = page.inner_text("body")
        # "已退出登录" (the toast) contains the substring "退出登录", so assert on
        # the visitor badge instead of the absence of the logout label.
        check("logout returns to visitor state", "只读访客" in body and "SSO 登录" in body, body[:80])
        shot(page, "06-loggedout")

        print("== 7. SSO login through the OIDC form ==")
        page.get_by_role("button", name="SSO 登录").click()
        page.wait_for_url("**/oidc/**", timeout=25000)
        page.wait_for_selector('input[name="password"]', timeout=20000)
        check("OIDC login page rendered", True, page.url)
        shot(page, "07-oidc-form")
        page.fill('input[name="password"]', OIDC_PASSWORD)
        page.click('button[type="submit"], input[type="submit"]')
        page.wait_for_timeout(4000)
        body = page.inner_text("body")
        check("back in the console after SSO", "Mission Control" in page.title() or "总览" in body, page.url)
        check("SSO identity badge", "已登录" in body, body[:100])
        shot(page, "08-sso-loggedin")

        context.close()
        browser.close()

    print()
    print("FAILURES:", failures or "none")
    print("screenshots:", sorted(p.name for p in OUT.glob("*.png")))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
