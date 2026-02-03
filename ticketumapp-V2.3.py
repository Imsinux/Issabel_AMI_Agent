import asyncio
import webbrowser
import json
import os
import sys
import time
import re
import logging
from panoramisk import Manager

# -------------------- Config --------------------
CONFIG_FILE = "settings.json"

# Ticketum Constants
NID = "1"
DEPT_ID = "1"

# برای اینکه با CDR بهتر جور دربیاد معمولاً linkedid بهتره
CDR_ID_SOURCE = "linkedid"  # "linkedid" یا "uniqueid"

# اگر تماس‌های داخلی هم باید Ticketum باز کنند
INCLUDE_INTERNAL_CALLS = True

# ---------- Dedup / Anti-duplicate settings ----------
RING_LOG_DEDUP_SEC = 15
ANSWER_DEDUP_SEC = 180
OPEN_DEDUP_SEC = 180

ring_log_seen = {}          # key -> timestamp
answer_seen = {}            # call_id_int -> timestamp
ticket_open_seen = {}       # call_id_int -> timestamp


def _allow_once(cache: dict, key, ttl_sec: int) -> bool:
    now = time.time()
    ts = cache.get(key)
    if ts is not None and (now - ts) < ttl_sec:
        return False
    cache[key] = now
    return True


def _cleanup_cache(cache: dict, ttl_sec: int) -> None:
    now = time.time()
    for k, ts in list(cache.items()):
        if (now - ts) > ttl_sec:
            cache.pop(k, None)


def load_or_create_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                print("✅ Settings file found. Loading configuration...")
                return json.load(f)
        except Exception as e:
            print(f"❌ Error reading settings file: {e}")
            sys.exit(1)
    else:
        print("⚠️  Settings file not found.")
        print("Please enter the server details below:")
        config = {}
        config["host"] = input("Server IP: ").strip()
        config["port"] = 5038
        config["username"] = input("AMI Username: ").strip()
        config["secret"] = input("AMI Password: ").strip()
        config["extension"] = input("Your Extension: ").strip()
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        return config


def _ext_in_str(ext: str, s: str) -> bool:
    s = s or ""
    return bool(re.search(rf"(?<!\d){re.escape(ext)}(?!\d)", s))


def is_call_for_my_ext(event, my_ext: str) -> bool:
    dest_chan = event.get("DestChannel", "") or ""
    dial_str = event.get("DialString", "") or ""
    dest_cid = event.get("DestCallerIDNum", "") or ""
    chan = event.get("Channel", "") or ""

    if dest_cid == my_ext:
        return True
    if _ext_in_str(my_ext, dest_chan):
        return True
    if _ext_in_str(my_ext, dial_str):
        return True
    if chan.startswith("Local/") and _ext_in_str(my_ext, chan):
        return True

    return False


def to_int_id(raw_id: str):
    """
    Asterisk ids like 1706871234.56 -> 170687123456 (int)
    """
    raw_id = (raw_id or "").strip()
    digits = re.sub(r"\D+", "", raw_id)  # keep only digits
    if not digits:
        return None
    return int(digits)


def get_call_id_int_for_cdr(event):
    """
    voip_id به صورت int (digits-only) برای URL.
    """
    linkedid = (event.get("Linkedid", "") or "").strip()
    uniqueid = (event.get("Uniqueid", "") or "").strip()

    if (CDR_ID_SOURCE or "linkedid").lower() == "uniqueid":
        raw = uniqueid or linkedid
    else:
        raw = linkedid or uniqueid

    return to_int_id(raw)


async def open_ticketum_async(loop, caller_id: str, ext_num: str, call_id_int: int) -> bool:
    """
    Open Ticketum only once per call_id_int.
    browser.open داخل executor تا loop بلاک نشه (کمک به Connection lost).
    """
    if call_id_int is None:
        return False

    if not _allow_once(ticket_open_seen, call_id_int, OPEN_DEDUP_SEC):
        return False

    url = f"https://ticketum.bki.ir/#/usersummary/{call_id_int}/{NID}/{DEPT_ID}/{caller_id}/{ext_num}"

    try:
        await loop.run_in_executor(None, webbrowser.open, url)
        # طبق درخواست شما: call_id_int چاپ نشود
        print(f"📞 ANSWERED! Caller: {caller_id} -> Opening Ticketum for ext {ext_num} ...")
        return True
    except Exception as e:
        print(f"❌ Error opening browser: {e}")
        return False


async def main():
    cfg = load_or_create_config()

    # logging (اختیاری)
    log = logging.getLogger("ami_listener")
    if not log.handlers:
        h = logging.StreamHandler()
        fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)

    manager = Manager(
        loop=asyncio.get_running_loop(),
        host=cfg["host"],
        port=cfg.get("port", 5038),
        username=cfg["username"],
        secret=cfg["secret"],

        # کمتر کردن حجم eventها (کمک به disconnect نشدن)
        events="call",

        # keepalive / reconnect
        ping_delay=10,
        ping_interval=10,
        reconnect_timeout=2,

        log=log,
    )

    print(f"⏳ Connecting to {cfg['host']}...")
    try:
        await manager.connect()
        print(f"✅ Connected! Watching for ANSWER on: {cfg['extension']}")
        print("--------------------------------------------------")
    except Exception as e:
        print(f"❌ Connection Failed: {e}")
        input("Press Enter to exit...")
        return

    # linkedid_raw -> caller_num
    pending_calls = {}
    pending_calls_ts = {}

    def cleanup_pending(ttl_sec=240):
        now = time.time()
        for k, t in list(pending_calls_ts.items()):
            if now - t > ttl_sec:
                pending_calls_ts.pop(k, None)
                pending_calls.pop(k, None)

    last_cleanup = 0.0

    def maybe_cleanup():
        nonlocal last_cleanup
        now = time.time()
        if now - last_cleanup < 5:
            return
        last_cleanup = now
        _cleanup_cache(ring_log_seen, RING_LOG_DEDUP_SEC * 4)
        _cleanup_cache(answer_seen, ANSWER_DEDUP_SEC * 2)
        _cleanup_cache(ticket_open_seen, OPEN_DEDUP_SEC * 2)
        cleanup_pending()

    @manager.register_event("DialBegin")
    async def on_dialbegin(manager, event):
        maybe_cleanup()

        my_ext = cfg["extension"]
        if not is_call_for_my_ext(event, my_ext):
            return

        caller_num = (event.get("CallerIDNum", "") or "").strip()
        linkedid_raw = (event.get("Linkedid", "") or event.get("Uniqueid", "") or "").strip()
        if not linkedid_raw:
            return

        if caller_num and caller_num != my_ext:
            if (linkedid_raw not in pending_calls) or (not pending_calls.get(linkedid_raw)):
                pending_calls[linkedid_raw] = caller_num
                pending_calls_ts[linkedid_raw] = time.time()

            ring_key = ("ring", linkedid_raw, my_ext)
            if _allow_once(ring_log_seen, ring_key, RING_LOG_DEDUP_SEC):
                print(f"🔔 Ringing {my_ext} from {caller_num} (saved, waiting for ANSWER)")

    @manager.register_event("DialEnd")
    async def on_dialend(manager, event):
        maybe_cleanup()

        my_ext = cfg["extension"]
        dial_status = (event.get("DialStatus", "") or "").upper()
        if dial_status != "ANSWER":
            return

        if not is_call_for_my_ext(event, my_ext):
            return

        linkedid_raw = (event.get("Linkedid", "") or event.get("Uniqueid", "") or "").strip()
        if not linkedid_raw:
            return

        call_id_int = get_call_id_int_for_cdr(event)
        if call_id_int is None:
            return

        # جلوگیری از چندبار اجرا شدن برای یک تماس
        if not _allow_once(answer_seen, call_id_int, ANSWER_DEDUP_SEC):
            return

        caller_num = pending_calls.pop(linkedid_raw, (event.get("CallerIDNum", "") or "").strip())
        pending_calls_ts.pop(linkedid_raw, None)

        if not caller_num or caller_num == my_ext:
            return

        if (INCLUDE_INTERNAL_CALLS is False) and (len(caller_num) <= 3):
            return

        # باز کردن Ticketum بدون بلاک کردن event loop
        asyncio.create_task(
            open_ticketum_async(asyncio.get_running_loop(), caller_num, my_ext, call_id_int)
        )

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        manager.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
