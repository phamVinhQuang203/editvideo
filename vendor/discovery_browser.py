# -*- coding: utf-8 -*-
"""Playwright child process used by the local video discovery page."""
import json
import os
import sys
import time
from datetime import datetime


PLATFORMS = {
    "douyin": {"login": "https://www.douyin.com/", "trending": "https://www.douyin.com/hot",
               "needle": "/video/"},
    "tiktok": {"login": "https://www.tiktok.com/login", "trending": "https://www.tiktok.com/explore",
               "needle": "/video/"},
    "bilibili": {"login": "https://passport.bilibili.com/login",
                 "trending": "https://www.bilibili.com/v/popular/all", "needle": "/video/"},
}


def browser_path():
    local = os.environ.get("LOCALAPPDATA", "")
    program = os.environ.get("PROGRAMFILES", "")
    program_x86 = os.environ.get("PROGRAMFILES(X86)", "")
    candidates = [os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
                  os.path.join(local, "CocCoc", "Browser", "Application", "browser.exe"),
                  os.path.join(program, "Google", "Chrome", "Application", "chrome.exe"),
                  os.path.join(program_x86, "Microsoft", "Edge", "Application", "msedge.exe")]
    return next((path for path in candidates if path and os.path.isfile(path)), None)


def open_context(playwright, profile):
    executable = browser_path()
    if not executable:
        raise RuntimeError("Không tìm thấy Chrome, Cốc Cốc hoặc Edge")
    return playwright.chromium.launch_persistent_context(
        profile, executable_path=executable, headless=False, viewport={"width": 1360, "height": 850},
        locale="vi-VN", args=["--disable-blink-features=AutomationControlled"])


def extract(page, platform):
    return page.evaluate(r"""({platform, needle}) => {
      const numberText = (node, labels) => {
        if (!node) return '';
        const text = (node.innerText || '').replace(/\s+/g, ' ');
        for (const label of labels) {
          const re = new RegExp('(\\d+(?:[.,]\\d+)?\\s*(?:万|亿|[kKmMwW])?)\\s*' + label, 'i');
          const m = text.match(re); if (m) return m[1];
        }
        return '';
      };
      const seen = new Set(), out = [];
      for (const a of document.querySelectorAll('a[href*="/video/"]')) {
        const href = a.href || ''; if (!href.includes(needle) || seen.has(href)) continue;
        seen.add(href);
        const card = platform === 'bilibili'
          ? (a.closest('.video-card,.popular-video-container,.rank-item') || a.parentElement?.parentElement || a)
          : (a.closest('article,li,[class*="card"],[class*="item"]') || a.parentElement || a);
        const img = card.querySelector('img');
        const titleNode = card.querySelector('.video-name,.video-card__info--tit,.video-card__info--title,[class*="title"],h3,h2,[title]') || a;
        const authorNode = card.querySelector('.up-name,.video-card__info--author,[class*="author"],[class*="user"]');
        const nums = [...(card.innerText || '').matchAll(/\d+(?:[.,]\d+)?\s*(?:万|亿|[kKmMwW])?/g)].map(x=>x[0]);
        out.push({url: href, title: titleNode.title || titleNode.innerText || img?.alt || '',
          author: authorNode?.innerText || '', thumbnail: img?.dataset?.src || img?.currentSrc || img?.src || '',
          views: card.querySelector('.play-text')?.innerText || numberText(card,['播放','观看','views?']) || nums[0] || '',
          likes: card.querySelector('.like-text')?.innerText || numberText(card,['点赞','获赞','likes?']) || nums[1] || '',
          comments: numberText(card,['评论','comments?']) || '', shares: numberText(card,['分享','shares?']) || ''});
        if (out.length >= 120) break;
      }
      return out;
    }""", {"platform": platform, "needle": PLATFORMS[platform]["needle"]})


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    if len(sys.argv) != 4 or sys.argv[1] not in ("login", "discover") or sys.argv[2] not in PLATFORMS:
        raise SystemExit("Tham số discovery browser không hợp lệ")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Thiếu Playwright. Hãy chạy lại setup.cmd hoặc: pip install playwright")
    action, platform, profile = sys.argv[1:]
    os.makedirs(profile, exist_ok=True)
    state_path = os.path.join(profile, ".discovery-browser.json")
    state = {"pid": os.getpid(), "action": action,
             "started_at": datetime.now().isoformat(timespec="seconds")}
    try:
        with open(state_path, encoding="utf-8") as handle:
            previous = json.load(handle)
        if previous.get("last_login"):
            state["last_login"] = previous["last_login"]
    except (OSError, ValueError, TypeError):
        pass
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False)
    with sync_playwright() as playwright:
        context = None
        try:
            context = open_context(playwright, profile)
            page = context.pages[0] if context.pages else context.new_page()
            if action == "login":
                page.goto(PLATFORMS[platform]["login"], wait_until="domcontentloaded", timeout=90000)
                page.bring_to_front()
                while context.pages:
                    time.sleep(1)
                state["last_login"] = datetime.now().isoformat(timespec="seconds")
                return
            page.goto(PLATFORMS[platform]["trending"], wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(6000)
            for _ in range(5):
                page.mouse.wheel(0, 1800); page.wait_for_timeout(800)
            items = extract(page, platform)
            warning = "" if items else "Không thấy video; hãy đăng nhập lại hoặc nền tảng đã đổi giao diện"
            print(json.dumps({"items": items, "warning": warning}, ensure_ascii=False))
        finally:
            if context:
                context.close()
            state.pop("pid", None); state.pop("action", None); state.pop("started_at", None)
            try:
                if state.get("last_login"):
                    with open(state_path, "w", encoding="utf-8") as handle:
                        json.dump(state, handle, ensure_ascii=False)
                elif os.path.isfile(state_path):
                    os.unlink(state_path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
