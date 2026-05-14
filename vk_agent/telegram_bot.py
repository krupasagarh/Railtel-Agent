"""
Telegram bot — Railtel (Railwire) only. Same Playwright flows as the combined VK bot.
"""
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
load_dotenv(os.path.join(ROOT_DIR, '.env'))

from multi_credentials import get_railwire_credentials, list_railwire_account_ids
from portal import (
    audit_subscriber_status,
    check_clear_customer_session,
    check_railtel_portal,
    close_railtel_browser,
    launch_railtel_browser,
    portal_audit_to_dict,
    railtel_login_once,
)
from request_log import log_bot_request
from bot_i18n import (
    T,
    lbl,
    get_lang,
    set_lang,
    clear_lang,
    is_pick_english,
    is_pick_kannada,
    reply_markup_language_select,
    reply_markup_portal_keyboard,
    reply_markup_menu_minimal,
    format_help_body,
    msg_with_labels,
    format_audit_result_for_chat,
    format_clear_result_for_chat,
    login_fail_message,
    welcome_language_prompt,
    remind_choose_language,
)


def looks_like_hathway_stb_id(text):
    t = (text or '').strip().upper()
    if re.fullmatch(r'N\d{11}', t):
        return True
    if re.fullmatch(r'T\d{12}', t):
        return True
    return False


def looks_like_railtel_phone(text):
    t = (text or '').strip()
    compact = re.sub(r'\s+', '', t)
    if re.fullmatch(r'\d{10}', compact):
        return True
    if re.fullmatch(r'\+91\d{10}', compact):
        return True
    return False


def looks_like_railtel_user_id(text):
    t = (text or '').strip()
    if len(t) < 4 or not t.lower().startswith('ka.'):
        return False
    rest = t[3:]
    if not rest:
        return False
    return bool(re.fullmatch(r'[A-Za-z0-9_.\-]+', rest))


def looks_like_railtel_subscriber_input(text):
    return looks_like_railtel_phone(text) or looks_like_railtel_user_id(text)


BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
if not BOT_TOKEN:
    raise RuntimeError('TELEGRAM_BOT_TOKEN is not set in .env')

BASE_URL = f'https://api.telegram.org/bot{BOT_TOKEN}'

_persistent_sessions = {}
_session_last_activity = {}
_chat_modes = {}
_chat_operator_rail = {}

_executors_guard = threading.Lock()
_chat_executors = {}
_idle_watchdog_stop = threading.Event()


def _effective_rail_account_id(chat_id):
    return _chat_operator_rail.get(chat_id)


def _handle_operator_account_commands(chat_id, t, lower, reply_id):
    if lower == '/rail_accounts':
        ids = list_railwire_account_ids()
        cur = _chat_operator_rail.get(chat_id)
        if not ids:
            send_message(
                chat_id,
                '<b>Railwire accounts</b>: none configured. Set <code>RAILWIRE_USER</code> / '
                '<code>RAILWIRE_PASS</code> or <code>RAILWIRE_ACCOUNTS_FILE</code> in <code>.env</code>.',
                reply_to_message_id=reply_id,
                parse_mode='HTML',
            )
            return True
        lines = ['<b>Railwire operator account ids</b>'] + [f'• <code>{_tg_escape(i)}</code>' for i in ids]
        lines.append(
            f'Current for this chat: <code>{_tg_escape(cur)}</code>'
            if cur
            else 'Current for this chat: <i>default</i> (see <code>RAILWIRE_DEFAULT_ACCOUNT_ID</code> or first row).'
        )
        lines.append('Set: <code>/rail_account YOUR_ID</code>')
        send_message(
            chat_id,
            '\n'.join(lines),
            reply_to_message_id=reply_id,
            parse_mode='HTML',
        )
        return True

    if lower.startswith('/rail_account'):
        if lower == '/rail_account':
            ids = list_railwire_account_ids()
            cur = _chat_operator_rail.get(chat_id)
            extra = '\n'.join(f'• <code>{_tg_escape(i)}</code>' for i in ids) if ids else '(none)'
            send_message(
                chat_id,
                '<b>Usage:</b> <code>/rail_account ID</code>\n'
                f'Current: <code>{_tg_escape(cur or "")}</code> (empty = default)\n\n{extra}',
                reply_to_message_id=reply_id,
                parse_mode='HTML',
            )
            return True
        parts = t.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            send_message(
                chat_id,
                '<b>Usage:</b> <code>/rail_account ID</code> — see <code>/rail_accounts</code>.',
                reply_to_message_id=reply_id,
                parse_mode='HTML',
            )
            return True
        acc = parts[1].strip()
        try:
            get_railwire_credentials(acc)
        except ValueError as exc:
            send_message(chat_id, _tg_escape(str(exc)), reply_to_message_id=reply_id)
            return True
        had_session = chat_id in _persistent_sessions
        if had_session:
            close_persistent_session(chat_id)
        _chat_operator_rail[chat_id] = acc
        msg = f'<b>Railwire operator for this chat:</b> <code>{_tg_escape(acc)}</code>.'
        if had_session:
            msg += '\n\nPrevious portal session was closed — start <b>Multi</b> again to log in with this account.'
        send_message(
            chat_id,
            msg,
            reply_to_message_id=reply_id,
            parse_mode='HTML',
            reply_markup=reply_markup_for_chat(chat_id),
        )
        return True

    return False


def _persistent_session_idle_seconds():
    raw = (os.getenv('TELEGRAM_SESSION_IDLE_SECONDS') or '300').strip()
    try:
        v = int(raw)
    except ValueError:
        v = 300
    return max(30, min(v, 86400))


def _get_chat_executor(chat_id):
    with _executors_guard:
        ex = _chat_executors.get(chat_id)
        if ex is None:
            ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f'chat-{chat_id}-')
            _chat_executors[chat_id] = ex
        return ex


def _shutdown_all_chat_executors():
    with _executors_guard:
        executors = list(_chat_executors.items())
        _chat_executors.clear()
    for cid, ex in executors:
        try:
            ex.shutdown(wait=True, cancel_futures=False)
        except Exception as e:
            print(f'Executor shutdown chat_id={cid}: {e}')


def _executor_noop():
    return None


def _graceful_restart_cleanup():
    print('Scheduled restart: draining per-chat task queues…')
    with _executors_guard:
        items = list(_chat_executors.items())
    for cid, ex in items:
        try:
            ex.submit(_executor_noop).result(timeout=300)
        except Exception as e:
            print(f'Scheduled restart drain chat_id={cid}: {e}')
    print('Scheduled restart: closing browser sessions…')
    for cid, ex in items:
        try:
            ex.submit(close_persistent_session, cid).result(timeout=120)
        except Exception as e:
            print(f'Scheduled restart close chat_id={cid}: {e}')
    print('Scheduled restart: shutting down executors…')
    _shutdown_all_chat_executors()


def _restart_process_same_argv():
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _run_handle_update(chat_id, text, reply_id):
    try:
        handle_update(chat_id, text, reply_id)
    except Exception as exc:
        print(f'handle_update error chat_id={chat_id}: {exc}')
        import traceback

        traceback.print_exception(type(exc), exc, exc.__traceback__)


def _tg_bool(v):
    return 'true' if v else 'false'


def _tg_escape(s):
    if s is None:
        return ''
    return html.escape(str(s), quote=False)


def telegram_request(method, params=None):
    url = f'{BASE_URL}/{method}'
    data = None
    if params is not None:
        data = urllib.parse.urlencode(params).encode('utf-8')
    with urllib.request.urlopen(url, data=data, timeout=120) as response:
        return json.loads(response.read().decode('utf-8'))


def reply_markup_for_chat(chat_id):
    return reply_markup_portal_keyboard(chat_id)


def reply_markup_remove():
    return json.dumps({'remove_keyboard': True})


def send_message(
    chat_id,
    text,
    reply_to_message_id=None,
    disable_notification=False,
    reply_markup=None,
    parse_mode='HTML',
):
    text = (text or '').strip()
    if len(text) > 4096:
        text = text[:4070] + '\n(truncated)'
    params = {
        'chat_id': str(chat_id),
        'text': text,
        'disable_web_page_preview': _tg_bool(True),
        'disable_notification': _tg_bool(disable_notification),
    }
    if parse_mode:
        params['parse_mode'] = parse_mode
    if reply_to_message_id is not None:
        params['reply_to_message_id'] = str(reply_to_message_id)
    if reply_markup is not None:
        params['reply_markup'] = reply_markup
    try:
        telegram_request('sendMessage', params)
    except urllib.error.HTTPError as exc:
        err_body = ''
        try:
            err_body = exc.read().decode('utf-8', errors='replace')
        except Exception:
            pass
        print(f'Telegram sendMessage HTTP {exc.code}: {err_body}')
        params.pop('parse_mode', None)
        try:
            telegram_request('sendMessage', params)
        except urllib.error.HTTPError as exc2:
            print(f'Telegram sendMessage retry failed HTTP {exc2.code}')
            raise


def get_mode(chat_id):
    return _chat_modes.get(chat_id, 'idle')


def set_mode(chat_id, mode):
    _chat_modes[chat_id] = mode


def close_persistent_session(chat_id):
    sess = _persistent_sessions.pop(chat_id, None)
    _session_last_activity.pop(chat_id, None)
    set_mode(chat_id, 'idle')
    if not sess:
        return
    try:
        close_railtel_browser(sess['playwright'], sess['browser'])
    except Exception as exc:
        print(f'close_persistent_session error: {exc}')


def start_multi_session(chat_id):
    close_persistent_session(chat_id)
    set_mode(chat_id, 'multi_pending')
    playwright, browser, page = launch_railtel_browser()
    rid = _effective_rail_account_id(chat_id)
    login_ok = railtel_login_once(page, account_id=rid)
    err_hint = login_fail_message(chat_id, 'railtel')
    try:
        if not login_ok:
            close_railtel_browser(playwright, browser)
            set_mode(chat_id, 'idle')
            return False, err_hint
        _persistent_sessions[chat_id] = {
            'playwright': playwright,
            'browser': browser,
            'page': page,
            'provider': 'railtel',
        }
        _session_last_activity[chat_id] = time.monotonic()
        set_mode(chat_id, 'multi')
        return True, ''
    except Exception as exc:
        try:
            close_railtel_browser(playwright, browser)
        except Exception:
            pass
        set_mode(chat_id, 'idle')
        return False, str(exc)


def _idle_watchdog_close(chat_id, last_at_schedule):
    idle_sec = _persistent_session_idle_seconds()
    if chat_id not in _persistent_sessions:
        return
    if _session_last_activity.get(chat_id) != last_at_schedule:
        return
    if time.monotonic() - last_at_schedule < idle_sec:
        return
    close_persistent_session(chat_id)
    try:
        send_message(
            chat_id,
            T(chat_id, 'session_idle_timeout'),
            reply_markup=reply_markup_for_chat(chat_id),
        )
    except Exception as exc:
        print(f'session_idle_timeout sendMessage chat_id={chat_id}: {exc}')


def _idle_watchdog_loop():
    interval = min(30, max(10, _persistent_session_idle_seconds() // 10))
    while not _idle_watchdog_stop.wait(interval):
        idle_sec = _persistent_session_idle_seconds()
        now = time.monotonic()
        for cid in list(_persistent_sessions.keys()):
            last = _session_last_activity.get(cid)
            if last is None:
                continue
            if (now - last) < idle_sec:
                continue
            try:
                _get_chat_executor(cid).submit(_idle_watchdog_close, cid, last)
            except Exception as exc:
                print(f'idle_watchdog submit chat_id={cid}: {exc}')


def format_help_for_chat(chat_id):
    return format_help_body(chat_id)


def parse_cid(text):
    text = text.strip()
    if not text:
        return None
    lower = text.lower()
    if lower.startswith('/audit'):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            return parts[1].strip()
        return None
    if lower.startswith('/start') or lower.startswith('/help'):
        return None
    if text.startswith('/'):
        return None
    if looks_like_railtel_subscriber_input(text):
        return text.strip()
    return None


def parse_subscriber_query(chat_id, text):
    t = text.strip()
    if not t:
        return None
    lower = t.lower()
    if lower.startswith('/audit'):
        parts = t.split(maxsplit=1)
        if len(parts) != 2:
            return None
        arg = parts[1].strip()
        if looks_like_hathway_stb_id(arg):
            return None
        return parse_cid(t)
    if lower.startswith('/start') or lower.startswith('/help'):
        return None
    if t.startswith('/'):
        return None
    if looks_like_hathway_stb_id(t):
        return None
    return parse_cid(t)


def get_updates(offset=None, timeout=30):
    params = {'timeout': timeout}
    if offset is not None:
        params['offset'] = offset
    result = telegram_request('getUpdates', params)
    if not result.get('ok'):
        raise RuntimeError('Telegram getUpdates failed: %s' % result)
    return result.get('result', [])


def drain_pending_updates(max_batches=50):
    if os.getenv('TELEGRAM_REPLAY_PENDING', '').strip().lower() in ('1', 'true', 'yes'):
        print('TELEGRAM_REPLAY_PENDING set — not skipping queued updates.')
        return None
    print('Skipping stale Telegram updates (queued before this process started)…')
    offset = None
    highest = None
    batches = 0
    while batches < max_batches:
        batches += 1
        params = {'timeout': 0}
        if offset is not None:
            params['offset'] = offset
        result = telegram_request('getUpdates', params)
        updates = result.get('result') or []
        if not updates:
            break
        batch_max = max(u['update_id'] for u in updates)
        highest = batch_max
        offset = batch_max + 1
    if highest is not None:
        print(f'   Cleared queue through update_id={highest}')
    return highest


def _is_done_command(chat_id, t, lower):
    if get_lang(chat_id):
        return t == lbl(chat_id, 'done') or lower in ('/done', '/logout')
    return lower in ('/done', '/logout')


def _is_menu_command(chat_id, t, lower):
    if get_lang(chat_id):
        return t == lbl(chat_id, 'menu') or lower == '/menu'
    return lower == '/menu'


def handle_update(chat_id, text, reply_id):
    t = (text or '').strip()
    lower = t.lower()
    mode = get_mode(chat_id)

    if is_pick_english(t):
        set_lang(chat_id, 'en')
        send_message(
            chat_id,
            f"{T(chat_id, 'language_set_en')}\n\n{msg_with_labels(chat_id, 'menu_main')}",
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_menu_minimal(chat_id),
        )
        return
    if is_pick_kannada(t):
        set_lang(chat_id, 'kn')
        send_message(
            chat_id,
            f"{T(chat_id, 'language_set_kn')}\n\n{msg_with_labels(chat_id, 'menu_main')}",
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_menu_minimal(chat_id),
        )
        return

    if get_lang(chat_id) is None:
        msg = welcome_language_prompt() if lower.startswith('/start') or lower.startswith('/help') else remind_choose_language()
        send_message(
            chat_id,
            msg,
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_language_select(chat_id),
        )
        return

    if chat_id in _persistent_sessions:
        idle_sec = _persistent_session_idle_seconds()
        last = _session_last_activity.get(chat_id, 0)
        if last > 0 and (time.monotonic() - last) >= idle_sec:
            close_persistent_session(chat_id)
            send_message(
                chat_id,
                T(chat_id, 'session_idle_timeout'),
                reply_to_message_id=reply_id,
                reply_markup=reply_markup_for_chat(chat_id),
            )
            return
        _session_last_activity[chat_id] = time.monotonic()

    if (t == lbl(chat_id, 'change_language')) or lower in ('/language', '/lang'):
        clear_lang(chat_id)
        send_message(
            chat_id,
            welcome_language_prompt(),
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_language_select(chat_id),
        )
        return

    if lower.startswith('/start') or lower.startswith('/help'):
        send_message(
            chat_id,
            format_help_for_chat(chat_id),
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_for_chat(chat_id),
        )
        return

    if _handle_operator_account_commands(chat_id, t, lower, reply_id):
        return

    if _is_menu_command(chat_id, t, lower):
        send_message(
            chat_id,
            msg_with_labels(chat_id, 'menu_main'),
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_menu_minimal(chat_id),
        )
        return

    if _is_done_command(chat_id, t, lower):
        close_persistent_session(chat_id)
        send_message(
            chat_id,
            T(chat_id, 'session_closed'),
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_for_chat(chat_id),
        )
        return

    is_multi = lower == '/multi' or t == lbl(chat_id, 'railtel_multi')
    if is_multi:
        send_message(
            chat_id,
            T(chat_id, 'logging_in_railtel'),
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_for_chat(chat_id),
        )
        ok, err = start_multi_session(chat_id)
        if ok:
            body = msg_with_labels(chat_id, 'logged_in_railtel', done=lbl(chat_id, 'done'))
            send_message(
                chat_id,
                body,
                reply_to_message_id=reply_id,
                reply_markup=reply_markup_for_chat(chat_id),
            )
        else:
            send_message(
                chat_id,
                T(chat_id, 'multi_start_fail', err=_tg_escape(err)),
                reply_to_message_id=reply_id,
                reply_markup=reply_markup_for_chat(chat_id),
            )
        return

    is_single = lower == '/single' or t == lbl(chat_id, 'railtel_single')
    if is_single:
        close_persistent_session(chat_id)
        set_mode(chat_id, 'single_wait')
        send_message(
            chat_id,
            T(chat_id, 'single_prompt_railtel'),
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_for_chat(chat_id),
        )
        return

    if t == lbl(chat_id, 'railtel_clear') or lower == '/clear':
        close_persistent_session(chat_id)
        set_mode(chat_id, 'clear_wait')
        send_message(
            chat_id,
            T(chat_id, 'clear_wait_prompt'),
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_for_chat(chat_id),
        )
        return

    cid = parse_subscriber_query(chat_id, t)

    if get_mode(chat_id) == 'multi_pending':
        if cid or looks_like_hathway_stb_id(t):
            send_message(
                chat_id,
                T(chat_id, 'multi_login_not_ready'),
                reply_to_message_id=reply_id,
                reply_markup=reply_markup_for_chat(chat_id),
            )
            return

    if mode == 'multi' and chat_id in _persistent_sessions:
        if not cid:
            if looks_like_hathway_stb_id(t):
                send_message(
                    chat_id,
                    msg_with_labels(chat_id, 'stb_on_railtel_multi'),
                    reply_to_message_id=reply_id,
                    reply_markup=reply_markup_for_chat(chat_id),
                )
            else:
                send_message(
                    chat_id,
                    msg_with_labels(chat_id, 'wait_railtel_multi', done=lbl(chat_id, 'done')),
                    reply_to_message_id=reply_id,
                    reply_markup=reply_markup_for_chat(chat_id),
                )
            return
        log_bot_request(action='multi_audit', identifier=cid, chat_id=chat_id)
        send_message(
            chat_id,
            T(chat_id, 'running_audit', cid=_tg_escape(cid)),
            reply_to_message_id=reply_id,
        )
        page = _persistent_sessions[chat_id]['page']
        audit = None
        try:
            audit = audit_subscriber_status(page, cid)
            reply_text = format_audit_result_for_chat(chat_id, cid, portal_audit_to_dict(audit))
        except Exception as exc:
            reply_text = T(chat_id, 'audit_error', exc=_tg_escape(exc))
        send_message(
            chat_id,
            reply_text,
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_for_chat(chat_id),
            parse_mode='HTML',
        )
        return

    if mode == 'single_wait':
        if not cid:
            if looks_like_hathway_stb_id(t):
                send_message(
                    chat_id,
                    msg_with_labels(chat_id, 'stb_on_railtel_single'),
                    reply_to_message_id=reply_id,
                    reply_markup=reply_markup_for_chat(chat_id),
                )
            else:
                send_message(
                    chat_id,
                    T(chat_id, 'single_wait_railtel'),
                    reply_to_message_id=reply_id,
                    reply_markup=reply_markup_for_chat(chat_id),
                )
            return
        log_bot_request(action='single_audit', identifier=cid, chat_id=chat_id)
        send_message(
            chat_id,
            T(chat_id, 'running_single_audit', cid=_tg_escape(cid)),
            reply_to_message_id=reply_id,
        )
        audit = check_railtel_portal(cid, account_id=_effective_rail_account_id(chat_id))
        set_mode(chat_id, 'idle')
        send_message(
            chat_id,
            format_audit_result_for_chat(chat_id, cid, audit),
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_for_chat(chat_id),
            parse_mode='HTML',
        )
        return

    if mode == 'clear_wait':
        if not cid:
            send_message(
                chat_id,
                T(chat_id, 'clear_send_id'),
                reply_to_message_id=reply_id,
                reply_markup=reply_markup_for_chat(chat_id),
            )
            return
        log_bot_request(action='clear_session', identifier=cid, chat_id=chat_id)
        send_message(
            chat_id,
            T(chat_id, 'clearing', cid=_tg_escape(cid)),
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_for_chat(chat_id),
        )
        try:
            result = check_clear_customer_session(cid, account_id=_effective_rail_account_id(chat_id))
            reply_text = format_clear_result_for_chat(chat_id, cid, result)
        except Exception as exc:
            reply_text = T(chat_id, 'clear_error', exc=_tg_escape(exc))
        set_mode(chat_id, 'idle')
        send_message(
            chat_id,
            reply_text,
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_for_chat(chat_id),
        )
        return

    if cid:
        log_bot_request(action='idle_quick_audit', identifier=cid, chat_id=chat_id)
        send_message(
            chat_id,
            T(chat_id, 'running_single_audit', cid=_tg_escape(cid)),
            reply_to_message_id=reply_id,
        )
        audit = check_railtel_portal(cid, account_id=_effective_rail_account_id(chat_id))
        send_message(
            chat_id,
            format_audit_result_for_chat(chat_id, cid, audit),
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_for_chat(chat_id),
            parse_mode='HTML',
        )
        return

    if not t:
        send_message(
            chat_id,
            format_help_for_chat(chat_id),
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_for_chat(chat_id),
        )
        return

    if looks_like_hathway_stb_id(t):
        send_message(
            chat_id,
            msg_with_labels(chat_id, 'stb_on_railtel_idle'),
            reply_to_message_id=reply_id,
            reply_markup=reply_markup_for_chat(chat_id),
        )
        return

    send_message(
        chat_id,
        T(chat_id, 'idle_prompt_railtel'),
        reply_to_message_id=reply_id,
        reply_markup=reply_markup_for_chat(chat_id),
    )


def main():
    pw_ms = int(os.getenv('PLAYWRIGHT_DEFAULT_TIMEOUT_MS', '300000'))
    _rs = os.getenv('BOT_RESTART_INTERVAL_SEC', str(2 * 3600))
    _rs = (_rs or '').strip()
    if not _rs:
        restart_sec = 2 * 3600
    else:
        try:
            restart_sec = max(0, int(_rs))
        except ValueError:
            print(f'Invalid BOT_RESTART_INTERVAL_SEC={_rs!r} — using default {2 * 3600}s.')
            restart_sec = 2 * 3600
    print(
        f'Starting Railtel Agent (per-chat Playwright thread; '
        f'PLAYWRIGHT_DEFAULT_TIMEOUT_MS={pw_ms})…'
    )
    idle_sec = _persistent_session_idle_seconds()
    print(
        f'Persistent portal sessions close after {idle_sec}s with no user messages '
        f'(TELEGRAM_SESSION_IDLE_SECONDS).'
    )
    if restart_sec > 0:
        print(f'Scheduled process restart every {restart_sec}s (BOT_RESTART_INTERVAL_SEC).')
    else:
        print('Scheduled process restart disabled (BOT_RESTART_INTERVAL_SEC is 0).')
    last_update_id = drain_pending_updates()
    started = time.monotonic()
    _idle_watchdog_stop.clear()
    threading.Thread(
        target=_idle_watchdog_loop,
        name='railtel-bot-session-idle',
        daemon=True,
    ).start()

    try:
        while True:
            try:
                if restart_sec > 0 and (time.monotonic() - started) >= restart_sec:
                    print(f'Uptime reached {restart_sec}s — restarting process.')
                    _graceful_restart_cleanup()
                    try:
                        _restart_process_same_argv()
                    except OSError as exc:
                        print(f'Process restart (execv) failed: {exc}')
                        raise
                updates = get_updates(offset=last_update_id + 1 if last_update_id is not None else None)
                if not updates:
                    time.sleep(1)
                    continue

                batch_max = last_update_id or 0
                for update in updates:
                    batch_max = max(batch_max, update['update_id'])
                    message = update.get('message') or update.get('edited_message')
                    if not message:
                        continue

                    chat_id = message['chat']['id']
                    text = message.get('text') or ''
                    reply_id = message.get('message_id')

                    _get_chat_executor(chat_id).submit(_run_handle_update, chat_id, text, reply_id)

                last_update_id = batch_max
                time.sleep(0.25)
            except Exception as exc:
                print(f'Bot error: {exc}')
                time.sleep(5)
    finally:
        _idle_watchdog_stop.set()
        print('Shutting down per-chat executors…')
        _shutdown_all_chat_executors()


if __name__ == '__main__':
    main()
