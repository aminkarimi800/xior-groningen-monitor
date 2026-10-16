# Temporary: try different browser strategies against the Cloudflare check on GitHub runners.
import sys
import time

URL = "https://www.xiorstudenthousing.eu/netherlands/groningen/"
COUNT_JS = "document.querySelectorAll('.filtered-homes-col h3').length"


def poll(get_title, get_count, label, seconds=60):
    for i in range(seconds // 3):
        time.sleep(3)
        try:
            title, n = get_title(), get_count()
        except Exception as e:
            title, n = f"<{e.__class__.__name__}>", 0
        print(f"[{label}] t={3 * (i + 1)}s title={title!r} cards={n}", flush=True)
        if n:
            print(f"RESULT {label}: PASS", flush=True)
            return True
    print(f"RESULT {label}: FAIL", flush=True)
    return False


def playwright_like(module, label, **launch):
    sync_playwright = __import__(module + ".sync_api", fromlist=["sync_playwright"]).sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context("/tmp/prof-" + label, channel="chrome", no_viewport=True, **launch)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
        poll(page.title, lambda: page.evaluate(COUNT_JS), label)
        ctx.close()


def seleniumbase_uc():
    from seleniumbase import SB
    with SB(uc=True, xvfb=True, locale="en") as sb:
        sb.uc_open_with_reconnect(URL, 6)
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print("click captcha:", e)
        poll(lambda: sb.get_title(), lambda: sb.execute_script("return " + COUNT_JS), "sb-uc")


strategy = sys.argv[1]
if strategy == "pw-chrome-headed":
    playwright_like("playwright", strategy, headless=False)
elif strategy == "patchright-headed":
    playwright_like("patchright", strategy, headless=False)
elif strategy == "patchright-headless":
    playwright_like("patchright", strategy, headless=True)
elif strategy == "sb-uc":
    seleniumbase_uc()
