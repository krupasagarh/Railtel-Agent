import argparse
import os
import re
import time
import pytesseract
from datetime import datetime
from PIL import Image, ImageEnhance, ImageOps
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from multi_credentials import get_railwire_credentials

# --- INITIALIZATION ---
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
load_dotenv(os.path.join(ROOT_DIR, '.env'))
# Ensure this path is correct for your Windows machine
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# Clear-session: reason modal + dropdown (portal may AJAX-load option list).
_CLEAR_REASON_MODAL_MS = int(os.getenv("RAILTEL_CLEAR_REASON_MODAL_MS", "5000"))
_CLEAR_REASON_DROPDOWN_MS = int(os.getenv("RAILTEL_CLEAR_REASON_DROPDOWN_MS", "4500"))
_CLEAR_REASON_OPTION_MS = int(os.getenv("RAILTEL_CLEAR_REASON_OPTION_MS", "1200"))
_CLEAR_RECONNECT_WAIT_SEC = int(os.getenv("RAILTEL_CLEAR_RECONNECT_SEC", "120"))
_CLEAR_AFTER_DISCONNECT_CLICK_MS = int(os.getenv("RAILTEL_CLEAR_AFTER_DISCONNECT_MS", "3000"))

_RAILTEL_REASON_MODAL_SETTLE_MS = int(os.getenv("RAILTEL_REASON_MODAL_SETTLE_MS", "900"))
_RAILTEL_REASON_AFTER_OPEN_MS = int(os.getenv("RAILTEL_REASON_AFTER_OPEN_MS", "700"))
_RAILTEL_REASON_OPTIONS_MAX_WAIT_MS = int(os.getenv("RAILTEL_REASON_OPTIONS_MAX_WAIT_MS", "12000"))
_RAILTEL_CLEAR_OK_POPUP_MS = int(os.getenv("RAILTEL_CLEAR_OK_POPUP_MS", "22000"))

# Banner cleanup after login — shorter waits; override via .env if the portal is slow.
_RAILTEL_BANNER_INITIAL_MS = int(os.getenv("RAILTEL_BANNER_INITIAL_MS", "600"))
_RAILTEL_BANNER_AFTER_CLICK_MS = int(os.getenv("RAILTEL_BANNER_AFTER_CLICK_MS", "450"))
_RAILTEL_BANNER_ELEMENT_WAIT_MS = int(os.getenv("RAILTEL_BANNER_ELEMENT_WAIT_MS", "5000"))


def solve_captcha(element, length=6):
    """Captures, enhances, and solves the Railtel CAPTCHA."""
    try:
        img_path = "railtel_captcha.png"
        element.screenshot(path=img_path)
        img = Image.open(img_path).convert('L')
        img = ImageEnhance.Contrast(img).enhance(2.5)
        img = img.point(lambda x: 0 if x < 128 else 255)
        text = pytesseract.image_to_string(img, config=r'--psm 6 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789').strip()
        return "".join(filter(str.isalnum, text))[:length]
    except Exception as e:
        print(f"OCR Error: {e}")
        return ""


def cleanup_railtel_banners(page):
    """Dismiss marketing / notice modals after dashboard load (kept short; tune via RAILTEL_BANNER_* env)."""
    initial = max(0, _RAILTEL_BANNER_INITIAL_MS)
    after_click = max(0, _RAILTEL_BANNER_AFTER_CLICK_MS)
    elem_wait = max(500, _RAILTEL_BANNER_ELEMENT_WAIT_MS)

    print("🧹 Banner cleanup…")
    if initial:
        page.wait_for_timeout(initial)

    def _click_if_visible(locator, label):
        try:
            if locator.count() == 0:
                return False
            first = locator.first
            first.wait_for(state="visible", timeout=elem_wait)
            first.scroll_into_view_if_needed(timeout=3000)
            first.click(timeout=4000, force=True)
            print(f"✅ {label}")
            if after_click:
                page.wait_for_timeout(after_click)
            return True
        except Exception as inner_e:
            print(f"⚠️ {label}: {inner_e}")
            return False

    try:
        _click_if_visible(page.locator("#closeBtn"), "Marketing banner closed.")
        _click_if_visible(
            page.locator("#modal-anp > div.modal-dialog > div > div.modal-header > button"),
            "Notice modal closed.",
        )

        fallback_selectors = [
            "button.close[data-dismiss='modal']",
            ".close-btn, .close-button, button[aria-label='Close']",
            "#modal-anp button.close, #modal-anp button[aria-label='Close']",
        ]
        for selector in fallback_selectors:
            loc = page.locator(selector).first
            try:
                if loc.count() == 0:
                    continue
                if not loc.is_visible(timeout=800):
                    continue
                loc.click(timeout=3000, force=True)
                print(f"✅ Fallback closed: {selector}")
                if after_click:
                    page.wait_for_timeout(after_click)
            except Exception as inner_e:
                print(f"⚠️ Fallback selector failed: {selector} -> {inner_e}")

        if os.getenv("RAILTEL_BANNER_CLEANUP_SCREENSHOT", "").strip().lower() in ("1", "true", "yes"):
            page.screenshot(path="banner_cleanup_debug.png")
            print("   📸 banner_cleanup_debug.png")
    except Exception as e:
        print(f"❌ Cleanup Error: {e}")
        import traceback
        traceback.print_exc()


def navigate_to_subscriber_list(page):
    """Navigates to the subscriber list via the 4th navbar item."""
    try:
        nav_selector = "#navbar-container > div.navbar-buttons.navbar-header.pull-right > ul > li:nth-child(4) > a"
        page.wait_for_selector(nav_selector, timeout=10000)
        page.locator(nav_selector).click()
        page.wait_for_timeout(3000)
        return True
    except Exception as e:
        print(f"⚠️ Navigation Error: {e}")
        return False


EXPIRY_HEADER_RE = re.compile(
    r'expiry|valid\s*(?:till|to|until|upto)|plan\s*exp|renew(?:al)?|expires?|account\s*exp',
    re.I,
)


def _looks_like_datetime_cell(text):
    if not text or len(text.strip()) < 8:
        return False
    t = text.strip()
    if re.search(r'\d{4}-\d{2}-\d{2}', t):
        return True
    if re.search(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', t):
        return True
    return False


def _format_expiry_display(raw):
    """Normalize portal expiry strings for display."""
    if not raw:
        return ''
    line = raw.strip().split('\n')[0].strip()
    if line.lower() in ('na', 'n/a', '-', '--', 'none'):
        return ''
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d/%m/%Y %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y %H:%M:%S', '%d-%m-%Y'):
        try:
            dt = datetime.strptime(line, fmt)
            if '%H' in fmt or '%M' in fmt:
                return dt.strftime('%d/%m/%y %I:%M:%S %p')
            return dt.strftime('%d/%m/%y')
        except ValueError:
            continue
    return raw.strip()


SUBSCRIPTION_EXPIRY_RE = re.compile(
    r'Subscription\s+Expiry\s*[:：]?\s*(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?)',
    re.I | re.DOTALL,
)


def extract_subscription_expiry_from_subscriber_details(page):
    """
    Reads plan expiry from the Subscriber Details block (label 'Subscription Expiry'),
    not from the AAA session usage row — matches the portal layout shown in subscriber view.
    """
    try:
        raw = page.evaluate(r"""() => {
            const t = document.body.innerText || '';
            const m = t.match(/Subscription\s+Expiry\s*[:：]?\s*(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?)/i);
            return m ? m[1].trim() : '';
        }""")
        if raw:
            return _format_expiry_display(raw)
    except Exception:
        pass
    try:
        html = page.content()
        m = SUBSCRIPTION_EXPIRY_RE.search(html)
        if m:
            return _format_expiry_display(m.group(1).strip())
    except Exception:
        pass
    return ''


def extract_expiry_from_aaa_sessions(page, start_time, session_dur, status_raw):
    """
    Fallback: reads expiry-like values from the #aaa_sessions usage table if
    Subscription Expiry was not found in Subscriber Details.
    """
    layout = page.evaluate("""() => {
        const root = document.querySelector('#aaa_sessions');
        if (!root) return { headers: [], cells: [] };
        const table = root.querySelector('table');
        if (!table) return { headers: [], cells: [] };
        let headers = [];
        const thead = table.querySelector('thead');
        if (thead) {
            const rows = thead.querySelectorAll('tr');
            const lastRow = rows[rows.length - 1];
            headers = [...lastRow.querySelectorAll('th')].map(h => h.innerText.replace(/\\s+/g, ' ').trim());
        }
        const firstBodyRow = table.querySelector('tbody tr');
        const cells = firstBodyRow
            ? [...firstBodyRow.querySelectorAll('td')].map(td => td.innerText.replace(/\\s+/g, ' ').trim())
            : [];
        return { headers, cells };
    }""")
    headers = layout.get('headers') or []
    cells = layout.get('cells') or []

    st = (start_time or '').strip()
    sd = (session_dur or '').strip()
    sr = (status_raw or '').strip()

    expiry_raw = ''
    if headers and cells:
        n = min(len(headers), len(cells))
        for i in range(n):
            if EXPIRY_HEADER_RE.search(headers[i]):
                expiry_raw = (cells[i] or '').strip()
                break

    if not expiry_raw:
        # Known positions: start=0, status=1, duration=2, MAC=6 — try 3,4,5 for plan/expiry.
        skip = {0, 1, 2, 6}
        for i, c in enumerate(cells):
            if i in skip:
                continue
            c = (c or '').strip()
            if not c or c in (st, sd, sr):
                continue
            if _looks_like_datetime_cell(c):
                expiry_raw = c
                break

    if not expiry_raw:
        for nth in (4, 5, 6):
            sel = f'#aaa_sessions > table > tbody > tr:nth-child(1) > td:nth-child({nth})'
            try:
                if page.locator(sel).count() == 0:
                    continue
                txt = page.locator(sel).first.inner_text().strip()
                if txt and txt not in (st, sd, sr) and _looks_like_datetime_cell(txt):
                    expiry_raw = txt
                    break
            except Exception:
                continue

    return _format_expiry_display(expiry_raw) if expiry_raw else ''


def goto_subscriber_billing_page(page, search_value):
    """
    From dashboard (logged in): open subscriber list, search, open details,
    open Billing / session table (#aaa_sessions). Returns matched_cid or None.
    """
    if not navigate_to_subscriber_list(page):
        print("⚠️ Could not reach subscriber list.")
        return None

    search_input = "#dynamic-table_filter > label > input"
    page.wait_for_selector(search_input, timeout=10000)
    page.fill(search_input, search_value)
    page.wait_for_timeout(2000)

    first_row = page.locator("#dynamic-table > tbody > tr").first
    if first_row.count() == 0 or "No matching records" in first_row.inner_text():
        print(f"❌ No results found for {search_value}.")
        return None

    matched_cid = first_row.locator("td:nth-child(2)").first.inner_text().strip()
    print(f"✅ Matched CID: {matched_cid}")
    first_row.locator("td:nth-child(2)").first.click()
    page.wait_for_timeout(2000)

    print("🔍 Locating Session Details link...")
    try:
        row_locator = page.locator("tr:has-text('View Data usage')")
        session_link = row_locator.locator("u:has-text('Click Here')").first
        session_link.wait_for(state="visible", timeout=10000)
        session_link.click(force=True)
        print("✅ Clicked the 1st 'Click Here' link.")
    except Exception:
        print("⚠️ Direct click failed, trying row-based JS fallback...")
        page.evaluate("""() => {
            const rows = [...document.querySelectorAll('tr')];
            const targetRow = rows.find(r => r.innerText.includes('View Data usage'));
            if (targetRow) {
                const link = targetRow.querySelector('u');
                if (link) link.click();
            }
        }""")

    page.wait_for_selector("#aaa_sessions", timeout=10000)
    print("✅ Entered Session Details Page.")
    return matched_cid


def audit_subscriber_status(page, search_value):
    """Searches subscriber details and extracts session data."""
    print(f"\n🔎 --- Starting Audit for: {search_value} ---")
    try:
        matched_cid = goto_subscriber_billing_page(page, search_value)
        if not matched_cid:
            return None

        start_time_sel = "#aaa_sessions > table > tbody > tr:nth-child(1) > td:nth-child(1)"
        status_sel = "#aaa_sessions > table > tbody > tr:nth-child(1) > td:nth-child(2)"
        time_sel = "#aaa_sessions > table > tbody > tr:nth-child(1) > td:nth-child(3)"
        mac_sel = "#aaa_sessions > table > tbody > tr:nth-child(1) > td:nth-child(7)"

        start_time = page.locator(start_time_sel).first.inner_text().strip()
        status_raw = page.locator(status_sel).first.inner_text().strip()
        session_dur = page.locator(time_sel).first.inner_text().strip()
        mac_address = page.locator(mac_sel).first.inner_text().strip()

        expiry_display = extract_subscription_expiry_from_subscriber_details(page)
        if not expiry_display:
            expiry_display = extract_expiry_from_aaa_sessions(page, start_time, session_dur, status_raw)

        is_online = "Disconnect" in status_raw
        days = 0
        if "days" in session_dur:
            try:
                days = int(session_dur.split()[0])
            except ValueError:
                days = 0

        def format_portal_time(value: str) -> str:
            try:
                dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
                return dt.strftime("%d/%m/%y %I:%M:%S %p")
            except Exception:
                return value

        formatted_start = format_portal_time(start_time)
        formatted_status = format_portal_time(status_raw) if status_raw and any(char.isdigit() for char in status_raw) else status_raw

        if is_online:
            time_desc = f"Active since {formatted_start}" if start_time else "Active Now"
        else:
            if status_raw and not status_raw.lower().startswith("na"):
                time_desc = f"Down since {formatted_status}"
            else:
                time_desc = f"Down since {formatted_start}" if start_time else "Down status unknown"

        return {
            "is_online": is_online,
            "session_days": days,
            "downtime": time_desc,
            "raw_time": session_dur,
            "status_text": status_raw,
            "start_time": start_time,
            "mac": mac_address,
            "expiry": expiry_display,
            "matched_cid": matched_cid,
            "search_value": search_value
        }
    except Exception as e:
        print(f"⚠️ Audit Critical Error: {e}")
        return None


def login_railwire(page, max_retries=3, account_id=None, user=None, password=None):
    """
    Core login sequence. Credentials: optional user/password overrides, else
    RAILWIRE_USER/PASS or RAILWIRE_ACCOUNTS_FILE (see multi_credentials).
    """
    if user is not None and password is not None:
        admin_user, admin_pass = user, password
    else:
        try:
            admin_user, admin_pass, _acc = get_railwire_credentials(account_id)
        except ValueError as exc:
            print(f"⚠️ Railwire credentials: {exc}")
            return False

    for attempt in range(1, max_retries + 1):
        print(f"\n[Railwire] Login Attempt {attempt}...")
        try:
            page.goto("https://ka.railwire.co.in/rlogin", wait_until="networkidle")
            page.locator("#username").fill(admin_user)
            page.locator("#password").fill(admin_pass)
            captcha_val = solve_captcha(page.locator("#captcha_code"))
            page.locator("#code").fill(captcha_val)
            page.locator("#btn_rlogin").click()

            try:
                page.wait_for_selector(".kt-header__topbar", timeout=15000)
                print("🎉 Login Recognized.")
                cleanup_railtel_banners(page)
                return True
            except Exception:
                if "rlogin" not in page.url:
                    print("🎉 Login Recognized (URL Redirect).")
                    cleanup_railtel_banners(page)
                    return True
            print("⚠️ Dashboard not reached. Retrying...")
        except Exception as e:
            print(f"⚠️ Attempt Error: {e}")

    return False


def portal_audit_to_dict(audit):
    """Normalize audit_subscriber_status output to API-style dict."""
    if not audit:
        return {"success": False, "error": "CID Not Found"}
    result = {
        "success": True,
        "is_online": audit["is_online"],
        "session_days": audit["session_days"],
        "downtime": audit["downtime"],
        "mac": audit.get("mac", "N/A"),
        "expiry": audit.get("expiry") or "",
        "matched_cid": audit.get("matched_cid"),
        "search_value": audit.get("search_value"),
    }
    if audit.get("matched_cid"):
        result["matched_cid"] = audit["matched_cid"]
    return result


def launch_railtel_browser(headless=False, slow_mo=None):
    """Start Playwright Chromium; caller must close browser and stop playwright."""
    if slow_mo is None:
        slow_mo = int(os.getenv("RAILTEL_SLOW_MO", "500"))
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=headless, slow_mo=slow_mo)
    page = browser.new_page()
    page.set_default_timeout(int(os.getenv("PLAYWRIGHT_DEFAULT_TIMEOUT_MS", "300000")))
    return playwright, browser, page


def close_railtel_browser(playwright, browser):
    try:
        browser.close()
    finally:
        playwright.stop()


def railtel_login_once(page, account_id=None):
    """Single login for a long-lived browser session (multi-customer mode)."""
    return login_railwire(page, account_id=account_id)


def check_railtel_portal(subscriber_id, account_id=None):
    """Logs in, audits one subscriber, closes browser (single-shot)."""
    playwright, browser, page = launch_railtel_browser()
    try:
        if not login_railwire(page, account_id=account_id):
            return {"success": False, "error": "Login Failed"}
        audit = audit_subscriber_status(page, subscriber_id)
        return portal_audit_to_dict(audit)
    except Exception as e:
        try:
            page.screenshot(path="portal_error.png")
        except Exception:
            pass
        return {"success": False, "error": str(e)}
    finally:
        close_railtel_browser(playwright, browser)


def _disconnect_reason_label():
    return (os.getenv("RAILTEL_DISCONNECT_REASON") or "slow browsing").strip()


def _is_disconnect_reason_placeholder(text):
    t = (text or "").strip().lower()
    if not t:
        return True
    if t in ("select", "select reason", "select reasons"):
        return True
    if t.startswith("select reason"):
        return True
    if t in ("--select--", "-- select --", "choose reason"):
        return True
    return False


def _pick_disconnect_reason_aggressive(page, reason_substr):
    """
    Last-resort: click matching Select2 row or set hidden <select> inside the reason modal.
    Handles portals where Playwright .click misses or options use role=treeitem.
    """
    reason = (reason_substr or "").strip()
    if not reason:
        return False
    try:
        return page.evaluate(
            """(reason) => {
                const want = (reason || '').toLowerCase().trim();
                const tokens = want.split(/[^\\w]+/).filter(w => w.length > 1);
                if (want && !tokens.includes(want)) tokens.unshift(want);
                const junk = (l) => !l || l === 'select reason' || l.startsWith('select reason')
                    || /searching|loading|please\\s*wait|no matches/i.test(l);

                const fireClick = (el) => {
                    try {
                        el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                        el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                        el.click();
                        return true;
                    } catch (e) { return false; }
                };

                const nodes = [...document.querySelectorAll(
                    '.select2-container--open .select2-results__option:not(.select2-results__message), '
                    + '.select2-container--open .select2-results__option[role="treeitem"], '
                    + '.select2-container--open .select2-results__option[role="option"], '
                    + '.select2-results li.select2-results__option, '
                    + '.dropdown-menu.show .dropdown-item, .dropdown-menu.show li'
                )];

                for (const el of nodes) {
                    const l = (el.textContent || '').trim().toLowerCase();
                    if (junk(l)) continue;
                    let ok = false;
                    if (want && l.includes(want)) ok = true;
                    else if (tokens.some(t => l.includes(t))) ok = true;
                    if (ok && fireClick(el)) return true;
                }

                const modals = [...document.querySelectorAll('.modal.show, .modal.in, .modal.fade.in')];
                const m = modals.reverse().find(x => /reason\\s+for\\s*disconnect/i.test(x.innerText || ''));
                if (!m) return false;
                const sel = m.querySelector('select');
                if (!sel || !sel.options || !sel.options.length) return false;
                for (let i = 0; i < sel.options.length; i++) {
                    const l = (sel.options[i].textContent || '').trim().toLowerCase();
                    if (junk(l)) continue;
                    if (!want || l.includes(want) || tokens.some(t => l.includes(t))) {
                        sel.selectedIndex = i;
                        sel.dispatchEvent(new Event('input', { bubbles: true }));
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                        if (window.jQuery && window.jQuery(sel).trigger) {
                            window.jQuery(sel).trigger('change');
                        }
                        return true;
                    }
                }
                return false;
            }""",
            reason,
        )
    except Exception:
        return False


def _disconnect_reason_still_placeholder(page):
    """True when the reason widget still shows the empty / placeholder label."""
    try:
        return page.evaluate("""() => {
            const modals = [...document.querySelectorAll('.modal.show, .modal.in, .modal.fade.in')];
            const m = modals.reverse().find(x => /reason\\s+for\\s*disconnect/i.test(x.innerText || ''));
            if (!m) return true;
            const rend = m.querySelector('.select2-selection__rendered, .select2-selection');
            const t = (rend && (rend.textContent || '').trim().toLowerCase()) || '';
            if (!t) return true;
            if (t.includes('select reason')) return true;
            return false;
        }""")
    except Exception:
        return True


def _simple_select_disconnect_reason_once(page, modal_locator, reason_label):
    """
    Single UX path: open the reason Select2 via its selection chrome (not the search box),
    wait for list rows, click the row matching RAILTEL_DISCONNECT_REASON — no typing.
    """
    reason = (reason_label or "slow browsing").strip()
    rl = reason.lower()
    tokens = [t for t in re.split(r"[^\w]+", rl) if len(t) > 1]
    if rl and rl not in tokens:
        tokens.insert(0, rl)
    tokens = list(dict.fromkeys(tokens))

    trigger = modal_locator.locator(".select2-container .select2-selection").first
    if trigger.count() == 0:
        trigger = modal_locator.locator(".select2-selection").first
    if trigger.count() == 0:
        print("   [clear-session] No Select2 .select2-selection in modal.", flush=True)
        return False

    try:
        trigger.scroll_into_view_if_needed(timeout=5000)
        trigger.click(timeout=8000, force=True)
    except Exception as exc:
        print(f"   [clear-session] Open reason list failed: {exc}", flush=True)
        return False

    pause = max(350, min(2000, _RAILTEL_REASON_AFTER_OPEN_MS))
    page.wait_for_timeout(pause)

    try:
        page.locator(".select2-container--open .select2-results").first.wait_for(
            state="visible", timeout=_CLEAR_REASON_DROPDOWN_MS
        )
    except Exception:
        print("   [clear-session] Select2 results list did not appear.", flush=True)
        return False

    _wait_disconnect_reason_options_ready(page, reason, _RAILTEL_REASON_OPTIONS_MAX_WAIT_MS)

    results = page.locator(".select2-container--open .select2-results")
    rows = results.locator(".select2-results__option")
    try:
        n = min(rows.count(), 40)
    except Exception:
        n = 0

    for i in range(n):
        row = rows.nth(i)
        try:
            raw = (row.inner_text(timeout=1500) or "").strip()
        except Exception:
            continue
        if _is_disconnect_reason_placeholder(raw):
            continue
        low = raw.lower()
        if any(x in low for x in ("searching", "loading", "please wait", "no matches")):
            continue
        if rl in low or any(t in low for t in tokens):
            try:
                row.click(timeout=8000, force=True)
                print(f"   [clear-session] Selected reason row: {raw[:100]!r}", flush=True)
                page.wait_for_timeout(450)
                return True
            except Exception as exc:
                print(f"   [clear-session] Reason row click failed: {exc}", flush=True)

    print("   [clear-session] No suitable reason row found in open list.", flush=True)
    return False


def _aaa_sessions_first_row_end_time_cell(page):
    return page.locator("#aaa_sessions table tbody tr").first.locator("td:nth-child(2)")


def _billing_row_shows_active_disconnect(page):
    """
    True when any session row shows an active End Time control (Disconnect / End-Session).
    Skips obvious totals/header rows; portal sometimes puts the live session below row 1.
    """
    try:
        return page.evaluate("""() => {
            const root = document.querySelector('#aaa_sessions');
            if (!root) return false;
            const rows = root.querySelectorAll('table tbody tr');
            for (const row of rows) {
                const rowText = (row.innerText || '').trim().toLowerCase();
                if (!rowText || /^total/i.test(rowText) || rowText.includes('total (mb)'))
                    continue;
                const tds = row.querySelectorAll('td');
                if (tds.length < 2) continue;
                const endCell = tds[1];
                const raw = (endCell.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                if (
                    raw.includes('disconnect')
                    && (raw.includes('session') || raw.includes('end-session') || raw.includes('end session'))
                ) return true;
                const clickable = endCell.querySelector('a, button, .btn, u');
                if (clickable && /disconnect/i.test(clickable.textContent || '')) return true;
            }
            return false;
        }""")
    except Exception:
        return False


def _refresh_session_table_after_clear(page, subscriber_search):
    """Billing table often stays stale until reload or re-navigation."""
    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("#aaa_sessions", timeout=20000)
        return True
    except Exception:
        try:
            goto_subscriber_billing_page(page, subscriber_search)
            return True
        except Exception:
            return False


def _top_modal_dialog(page, title_regex=re.compile(r"Reason\s+for\s+disconnect", re.I)):
    """Front-most modal-dialog that shows the disconnect-reason popup."""
    chains = (
        page.locator(
            "div.modal.in div.modal-dialog, div.modal.show div.modal-dialog, "
            "div.modal.fade.in div.modal-dialog"
        ).filter(has_text=title_regex),
        page.locator("div.modal-dialog").filter(has_text=title_regex),
        page.locator(".modal-content:visible").filter(has_text=title_regex),
        page.locator("[role='dialog']").filter(has_text=title_regex),
    )
    scoped = None
    for loc in chains:
        try:
            if loc.count() > 0:
                scoped = loc
                break
        except Exception:
            continue
    if scoped is None:
        raise RuntimeError("Could not find Reason-for-disconnect modal in DOM.")
    scoped.last.wait_for(state="visible", timeout=_CLEAR_REASON_MODAL_MS)
    return scoped.last


def _wait_any_disconnect_reason_ui(page):
    """Wait until the reason popup is actually present (heading or body text)."""
    page.get_by_text(re.compile(r"Reason\s+for\s+disconnect", re.I)).first.wait_for(
        state="visible", timeout=_CLEAR_REASON_MODAL_MS
    )


def _wait_reason_dropdown_open(page, timeout_ms=None):
    """Wait for Select2 / Bootstrap dropdown list — no fixed sleep."""
    if timeout_ms is None:
        timeout_ms = _CLEAR_REASON_DROPDOWN_MS
    try:
        page.locator(
            ".select2-container--open .select2-results, "
            ".dropdown-menu.show, .dropdown-menu.open, "
            "[role='listbox']:visible"
        ).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _wait_disconnect_reason_options_ready(page, reason_substr, max_wait_ms=None):
    """
    After the reason dropdown is open, wait until options look loaded (not only Searching/Loading).
    """
    if max_wait_ms is None:
        max_wait_ms = _RAILTEL_REASON_OPTIONS_MAX_WAIT_MS
    max_wait_ms = max(400, min(int(max_wait_ms), 60000))
    want = (reason_substr or "").strip().lower()
    deadline = time.monotonic() + max_wait_ms / 1000.0
    while time.monotonic() < deadline:
        state = page.evaluate(
            """(want) => {
                const wantL = (want || '').toLowerCase();
                const nodes = [...document.querySelectorAll(
                    '.select2-container--open .select2-results__option, '
                    + '.select2-container--open li[role="option"], '
                    + '.dropdown-menu.show li, .dropdown-menu.show a.dropdown-item'
                )];
                let anySubstantial = false;
                let matchFound = false;
                for (const n of nodes) {
                    const t = (n.textContent || '').trim();
                    if (!t) continue;
                    const l = t.toLowerCase();
                    if (l === 'select reason' || l.startsWith('select reason')) continue;
                    if (/searching|loading|please\\s*wait|no matches/i.test(l)) return 'loading';
                    if (wantL && l.includes(wantL)) matchFound = true;
                    if (t.length > 2) anySubstantial = true;
                }
                if (matchFound) return 'ready_match';
                if (anySubstantial) return 'ready_any';
                return 'empty';
            }""",
            want,
        )
        if state in ("ready_match", "ready_any"):
            return True
        page.wait_for_timeout(180)
    return False


def _click_disconnect_confirm(page, modal_dialog):
    """Blue Disconnect on reason modal (avoid Cancel)."""
    page.wait_for_timeout(400)
    disconnect_btns = modal_dialog.locator(
        "button:not([disabled]):visible, a.btn:not([disabled]):visible"
    ).filter(has_text=re.compile(r"^\s*Disconnect\s*$", re.I))
    if disconnect_btns.count() == 0:
        disconnect_btns = modal_dialog.locator(
            "button.btn-primary:not([disabled]), button.btn-info:not([disabled]), "
            "button[type='submit']:not([disabled])"
        ).filter(has_text=re.compile(r"Disconnect", re.I))
    if disconnect_btns.count() == 0:
        disconnect_btns = modal_dialog.locator("button:visible").filter(
            has_text=re.compile(r"Disconnect", re.I)
        )
    try:
        if disconnect_btns.count() > 0:
            disconnect_btns.first.click(force=True, timeout=15000)
            return
    except Exception:
        pass
    page.evaluate("""() => {
        const modals = [...document.querySelectorAll('.modal.in, .modal.show, .modal.fade.in, [role="dialog"]')];
        const m = modals.reverse().find(x => /reason\\s+for\\s+disconnect/i.test(x.innerText || ''));
        if (!m) return false;
        const buttons = [...m.querySelectorAll('button, a.btn')];
        let btn = buttons.find(b => /^\\s*Disconnect\\s*$/i.test((b.textContent || '').trim()));
        if (!btn) btn = buttons.find(b => /disconnect/i.test(b.textContent || '') && !/cancel/i.test(b.textContent || ''));
        if (btn) { btn.click(); return true; }
        return false;
    }""")


def _click_ok_second_popup(page):
    """
    After blue Disconnect on the reason modal, dismiss the next portal dialog (Bootbox / Swal2 / Bootstrap).
    Copy varies by release — we poll for a visible OK-style button instead of matching one exact sentence.
    """
    page.wait_for_timeout(900)
    deadline = time.monotonic() + max(10.0, _RAILTEL_CLEAR_OK_POPUP_MS / 1000.0)
    while time.monotonic() < deadline:
        clicked = page.evaluate(
            """() => {
                const roots = [...document.querySelectorAll(
                    '.bootbox.in .modal-dialog, .bootbox.modal .modal-dialog, '
                    + '.swal2-container.swal2-shown, .swal2-container, '
                    + '.sweet-alert.visible, .sweet-alert.show, .sweet-alert, '
                    + 'div.modal.show .modal-dialog, div.modal.in .modal-dialog, '
                    + '[role="dialog"]'
                )];
                for (const r of roots) {
                    if (!r || !r.querySelectorAll) continue;
                    const st = window.getComputedStyle(r);
                    if (st.display === 'none' || st.visibility === 'hidden' || Number.parseFloat(st.opacity || '1') === 0)
                        continue;
                    const btns = [...r.querySelectorAll('button, a.btn')];
                    for (const b of btns) {
                        const t = (b.textContent || '').trim();
                        const l = t.toLowerCase();
                        if (!l || l === 'cancel' || l === 'close' || l === 'no') continue;
                        if (/^\\s*ok\\s*$/i.test(t) || /^\\s*yes\\s*$/i.test(t) || l === 'confirm' || l === 'done'
                            || /^continue$/i.test(t) || /^proceed$/i.test(t)) {
                            try { b.click(); return t.slice(0, 48); } catch (e) {}
                        }
                    }
                    const bp = r.querySelector(
                        'button.btn-primary, button.btn-success, .swal2-confirm, .btn.btn-primary'
                    );
                    if (bp) {
                        const t = (bp.textContent || '').trim();
                        if (t && /ok|yes|confirm|continue|proceed/i.test(t) && !/cancel/i.test(t)) {
                            try { bp.click(); return t.slice(0, 48); } catch (e) {}
                        }
                    }
                }
                return '';
            }"""
        )
        if clicked:
            print(f"   [clear-session] Follow-up dialog OK: {clicked!r}", flush=True)
            return True
        page.wait_for_timeout(400)

    chains = [
        page.locator(".bootbox .modal-dialog button.btn-primary"),
        page.locator(".swal2-popup button.swal2-confirm"),
        page.locator(".sweet-alert button.confirm"),
        page.locator(".modal.show button.btn-success:visible"),
        page.locator(".modal.show button:visible").filter(has_text=re.compile(r"^\s*OK\s*$", re.I)),
        page.get_by_role("button", name=re.compile(r"^\s*OK\s*$", re.I)),
        page.get_by_role("button", name=re.compile(r"^\s*(Continue|Proceed)\s*$", re.I)),
    ]
    for loc in chains:
        try:
            if loc.count() == 0:
                continue
            btn = loc.first
            btn.wait_for(state="visible", timeout=5000)
            btn.click(force=True, timeout=8000)
            print("   [clear-session] Follow-up OK via Playwright chain.", flush=True)
            return True
        except Exception:
            continue

    swept = page.evaluate("""() => {
        const roots = [...document.querySelectorAll(
            '.modal.show, .modal.in, .swal2-container, .sweet-alert, .bootbox, [role="dialog"]'
        )];
        for (const r of roots) {
            const btn = [...r.querySelectorAll('button')].find(b => {
                const t = (b.textContent || '').trim();
                return /^\\s*OK\\s*$/i.test(t) || /^\\s*Yes\\s*$/i.test(t)
                    || /^Continue$/i.test(t) || /^Proceed$/i.test(t);
            });
            if (btn) { btn.click(); return true; }
            const g = r.querySelector('.btn-success, .swal2-confirm, .btn-primary');
            if (g && /ok|yes|continue|proceed/i.test((g.textContent || '').trim())) { g.click(); return true; }
        }
        return false;
    }""")
    if swept:
        print("   [clear-session] Follow-up OK (final JS sweep).", flush=True)
        return True
    raise RuntimeError(
        "Follow-up dialog: no OK/Yes/Continue/Proceed button found. "
        "Try raising RAILTEL_CLEAR_OK_POPUP_MS; screenshot clear_session_ok_popup_miss.png if enabled."
    )


def clear_customer_session_portal(page, subscriber_id):
    """
    End subscriber session from Billing Data table: Disconnect/End-Session → reason modal → OK on wait popup,
    then wait until an active session shows Disconnect again (~within 1 minute per portal).
    """
    reason = _disconnect_reason_label()
    print(f"⚡ Clear session for {subscriber_id} (reason={reason!r})...")
    try:
        matched_cid = goto_subscriber_billing_page(page, subscriber_id)
        if not matched_cid:
            return {"success": False, "error": "Subscriber not found or could not open billing page."}

        print("   [clear-session] Checking #aaa_sessions for an active Disconnect control…")
        if not _billing_row_shows_active_disconnect(page):
            try:
                page.screenshot(path="clear_session_no_disconnect.png")
            except Exception:
                pass
            return {
                "success": False,
                "error": "No active session — End Time does not show Disconnect/End-Session.",
                "matched_cid": matched_cid,
            }

        sessions_root = page.locator("#aaa_sessions")
        # Portal copy varies: "Disconnect/End-Session", "Disconnect / End Session", or plain "Disconnect".
        disc_full = sessions_root.get_by_text(
            re.compile(r"Disconnect\s*/\s*End[-\s]*Session", re.I)
        ).first
        disc_plain = sessions_root.get_by_text(re.compile(r"Disconnect", re.I)).first
        disconnect_btn = disc_full.or_(disc_plain)

        print("   [clear-session] Waiting for Disconnect control (up to 20s)…")
        disconnect_btn.wait_for(state="visible", timeout=20000)
        print("   [clear-session] Clicking Disconnect / End-Session…")
        disconnect_btn.click(force=True)
        # Let the reason modal / overlay settle before interacting with the dropdown.
        print(f"   [clear-session] Waiting {_CLEAR_AFTER_DISCONNECT_CLICK_MS}ms for reason modal…")
        page.wait_for_timeout(_CLEAR_AFTER_DISCONNECT_CLICK_MS)

        try:
            page.screenshot(path="clear_session_after_disconnect_click.png")
        except Exception:
            pass

        print("   [clear-session] Waiting for 'Reason for disconnect' modal…")
        _wait_any_disconnect_reason_ui(page)
        reason_modal = _top_modal_dialog(page)
        try:
            page.screenshot(path="clear_session_reason_modal.png")
        except Exception:
            pass

        print("   [clear-session] Choosing disconnect reason in modal…", flush=True)
        settle = max(0, _RAILTEL_REASON_MODAL_SETTLE_MS)
        print(f"   [clear-session] Modal settle sleep: {settle}ms (RAILTEL_REASON_MODAL_SETTLE_MS)", flush=True)
        if settle:
            page.wait_for_timeout(settle)

        picked_native = False
        native_sel = reason_modal.locator("select").first
        try:
            n_cnt = native_sel.count()
        except Exception:
            n_cnt = 0
        if n_cnt > 0:
            native_visible = False
            try:
                native_visible = native_sel.is_visible(timeout=2500)
            except Exception:
                native_visible = False
            if native_visible:
                print("   [clear-session] Trying visible native <select> (no search UI)…", flush=True)
                try:
                    native_sel.select_option(label=reason, timeout=10000)
                    picked_native = True
                    print("   [clear-session] Native select OK.", flush=True)
                except Exception:
                    try:
                        opts = native_sel.evaluate(
                            """el => [...el.options].map(o => ({ text: o.text, value: o.value }))"""
                        )
                        for i, o in enumerate(opts or []):
                            if reason.lower() in (o.get("text") or "").lower():
                                native_sel.select_option(index=i, timeout=10000)
                                picked_native = True
                                print("   [clear-session] Native select by index OK.", flush=True)
                                break
                    except Exception:
                        picked_native = False

        if not picked_native:
            print(
                "   [clear-session] Select2: open list → click reason row (no search / no typing)…",
                flush=True,
            )
            picked_native = _simple_select_disconnect_reason_once(page, reason_modal, reason)
        if not picked_native:
            print("   [clear-session] Retrying simple reason pick once…", flush=True)
            picked_native = _simple_select_disconnect_reason_once(page, reason_modal, reason)
        if not picked_native and _disconnect_reason_still_placeholder(page):
            print("   [clear-session] Fallback: set hidden <select> + click list via DOM…", flush=True)
            picked_native = _pick_disconnect_reason_aggressive(page, reason)

        page.wait_for_timeout(500)

        try:
            page.screenshot(path="clear_session_after_reason_select.png")
        except Exception:
            pass

        print("   [clear-session] Clicking blue Disconnect on reason modal…", flush=True)
        _click_disconnect_confirm(page, reason_modal)

        print("   [clear-session] Waiting for follow-up dialog, then OK…", flush=True)
        page.wait_for_timeout(1200)
        try:
            _click_ok_second_popup(page)
        except Exception as ok_exc:
            print(f"   [clear-session] OK popup step: {ok_exc}")
            try:
                page.screenshot(path="clear_session_ok_popup_miss.png")
            except Exception:
                pass

        # After disconnect the billing grid often does not update until reload/re-navigation.
        page.wait_for_timeout(2500)

        deadline = time.time() + _CLEAR_RECONNECT_WAIT_SEC
        while time.time() < deadline:
            _refresh_session_table_after_clear(page, subscriber_id)
            if _billing_row_shows_active_disconnect(page):
                return {
                    "success": True,
                    "matched_cid": matched_cid,
                    "message": "Session cleared; subscriber shows an active session (Disconnect/End-Session).",
                }
            page.wait_for_timeout(2500)

        return {
            "success": False,
            "error": (
                f"No active Disconnect/End-Session detected within {_CLEAR_RECONNECT_WAIT_SEC}s "
                "(page refreshed each attempt). If reconnection definitely succeeded, try raising "
                "RAILTEL_CLEAR_RECONNECT_SEC in .env."
            ),
            "matched_cid": matched_cid,
        }
    except Exception as e:
        try:
            page.screenshot(path="clear_session_error.png")
        except Exception:
            pass
        print(f"⚠️ Clear session error: {e}")
        return {"success": False, "error": str(e)}


def check_clear_customer_session(subscriber_id, account_id=None):
    """Login, clear session flow, close browser (single-shot)."""
    # No slow_mo here — adds ~500ms per Playwright action and makes the reason modal feel stuck.
    clear_slow = int(os.getenv("RAILTEL_CLEAR_SLOW_MO", "0"))
    playwright, browser, page = launch_railtel_browser(slow_mo=clear_slow)
    try:
        if not login_railwire(page, account_id=account_id):
            return {"success": False, "error": "Login Failed"}
        return clear_customer_session_portal(page, subscriber_id)
    except Exception as e:
        try:
            page.screenshot(path="portal_clear_error.png")
        except Exception:
            pass
        return {"success": False, "error": str(e)}
    finally:
        close_railtel_browser(playwright, browser)


def clear_customer_session(subscriber_id):
    """Legacy one-shot placeholder."""
    print(f"⚡ Requesting session refresh for {subscriber_id}...")
    return True


customers_to_check = [
    "ka.hemantgk",
    "ka.shashank12",
    "ka.varun12",
    "ka.salma",
    "ka.yogeshp",
    "ka.krupasagar.h"
]


def run_agent(target_cid=None):
    master_results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=500)
        context = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = context.new_page()

        if login_railwire(page):
            if target_cid:
                print(f"\n🚀 Auditing single CID: {target_cid}")
                result = audit_subscriber_status(page, target_cid)
                if result:
                    master_results.append({
                        'id': target_cid,
                        'status': '🟢 ACTIVE' if result['is_online'] else '🔴 INACTIVE',
                        'mac': result.get('mac', 'N/A'),
                        'time': result['downtime'],
                        'expiry': result.get('expiry') or ''
                    })
            else:
                print("\n🚀 Starting Stabilized Bulk Audit...")
                for index, cid in enumerate(customers_to_check, 1):
                    result = audit_subscriber_status(page, cid)
                    if result:
                        master_results.append({
                            'id': cid,
                            'status': '🟢 ACTIVE' if result['is_online'] else '🔴 INACTIVE',
                            'mac': result.get('mac', 'N/A'),
                            'time': result['downtime'],
                            'expiry': result.get('expiry') or ''
                        })
                    page.wait_for_timeout(1000)

            if master_results:
                print("\n" + "="*100)
                print("📜 FINAL AUDIT SUMMARY (Copy Ready)")
                print("="*100)
                print(f"{'Sl.No':<6} | {'ID':<18} | {'Status':<12} | {'MAC':<18} | {'Expiry':<14} | {'Session Time'}")
                print("-" * 100)
                for index, r in enumerate(master_results, 1):
                    exp = (r.get('expiry') or '')[:14]
                    print(f"{index:<6} | {r['id']:<18} | {r['status']:<12} | {r['mac']:<18} | {exp:<14} | {r['time']}")
                print("="*100)
        browser.close()


def main():
    parser = argparse.ArgumentParser(description='Railtel portal audit runner')
    parser.add_argument('cid', nargs='?', help='Optional subscriber CID to audit')
    args = parser.parse_args()
    run_agent(target_cid=args.cid)


if __name__ == '__main__':
    main()