import asyncio
import webbrowser
import json
import os
import sys
import time
import re
import logging
from pathlib import Path
from panoramisk import Manager

# ============================================================
#  - No input()/console prompts
#  - Logs to app.log
#  - settings.json - Config
# ============================================================

# -------------------- Paths (work in .py or frozen .exe) --------------------
BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_FILE = str(BASE_DIR / "settings.json")
LOG_FILE = str(BASE_DIR / "app.log")

# -------------------- Logging --------------------
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("ticketum_ami")

# -------------------- Ticketum Config --------------------
DEPT_ID = "1"

CDR_ID_SOURCE = "linkedid"

INCLUDE_INTERNAL_CALLS = True

RING_LOG_DEDUP_SEC = 15
ANSWER_DEDUP_SEC = 180
OPEN_DEDUP_SEC = 180

ring_log_seen = {}          # key -> timestamp
answer_seen = {}            # call_id_int -> timestamp
ticket_open_seen = {}       # call_id_int -> timestamp

def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        log.exception("Failed to remove lock file")


# -------------------- Helpers --------------------
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
    """
    No interactive prompts (since we run without console).
    If settings.json doesn't exist, create a template and exit.
    """
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)

            # Basic validation
            required = ["host", "username", "secret", "extension"]
            missing = [k for k in required if not str(cfg.get(k, "")).strip()]
            if missing:
                log.error(f"settings.json is missing required fields: {missing}. Fix and restart.")
                raise ValueError("Invalid config")

            cfg.setdefault("port", 5038)
            return cfg
        except Exception:
            log.exception("Error reading/validating settings.json")
            raise

    # Create template
    template = {
        "host": "192.168.202.20",
        "port": 5038,
        "username": "",
        "secret": "",
        "extension": ""
    }
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=4)
        log.error("settings.json was missing. Template created next to the EXE. Fill it and restart.")
    except Exception:
        log.exception("Failed to create settings.json template")

    raise SystemExit(1)


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
    voip_id as int (digits-only) for URL.
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
    Use executor so we don't block the loop.
    """
    if call_id_int is None:
        return False

    if not _allow_once(ticket_open_seen, call_id_int, OPEN_DEDUP_SEC):
        return False

    url = f"https://ticketum.bki.ir/#/usersummary/{call_id_int}/{DEPT_ID}/{caller_id}/{ext_num}"

    try:
        await loop.run_in_executor(None, webbrowser.open, url)
        log.info(f"ANSWERED: Caller={caller_id} Ext={ext_num} -> Ticketum opened")
        return True
    except Exception:
        log.exception("Error opening browser")
        return False


async def main():
    cfg = load_or_create_config()

    
    log.info("--------------------------------------------------")
    log.info("App started")
    log.info(f"Base dir: {BASE_DIR}")
    log.info(f"Watching for ANSWER on extension: {cfg['extension']} at {cfg['host']}:{cfg.get('port', 5038)}")

    manager = Manager(
        loop=asyncio.get_running_loop(),
        host=cfg["host"],
        port=cfg.get("port", 5038),
        username=cfg["username"],
        secret=cfg["secret"],

        # reduce event volume (helps disconnect issues)
        events="call",

        # keepalive / reconnect
        ping_delay=10,
        ping_interval=10,
        reconnect_timeout=2,

        log=log,
    )

    try:
        await manager.connect()
        log.info("Connected to AMI successfully")
    except Exception:
        log.exception("Connection failed")
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
    async def on_dialbegin(_manager, event):
        try:
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
                    log.info(f"RING: Ext={my_ext} From={caller_num} (saved, waiting for ANSWER)")
        except Exception:
            log.exception("Error handling DialBegin")

    @manager.register_event("DialEnd")
    async def on_dialend(_manager, event):
        try:
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

            # prevent multiple triggers for same call
            if not _allow_once(answer_seen, call_id_int, ANSWER_DEDUP_SEC):
                return

            caller_num = pending_calls.pop(linkedid_raw, (event.get("CallerIDNum", "") or "").strip())
            pending_calls_ts.pop(linkedid_raw, None)

            if not caller_num or caller_num == my_ext:
                return

            # If internal calls should NOT open Ticketum:
            if (INCLUDE_INTERNAL_CALLS is False) and (len(caller_num) <= 3):
                return

            # open Ticketum without blocking the loop
            asyncio.create_task(
                open_ticketum_async(asyncio.get_running_loop(), caller_num, my_ext, call_id_int)
            )
        except Exception:
            log.exception("Error handling DialEnd")

    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("Main loop error")
    finally:
        try:
            manager.close()
        except Exception:
            log.exception("Error closing manager")
        log.info("App stopped")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main())
