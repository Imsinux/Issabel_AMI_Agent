import asyncio
import webbrowser
import json
import os
import sys
import time
import re
from panoramisk import Manager

# Configuration file name
CONFIG_FILE = 'settings.json'

# Fixed Ticketum Constants
VOIP_ID = "ISB-901"
NID = "11111111110"
DEPT_ID = "1"

# ---------- Dedup / Anti-duplicate settings ----------
RING_LOG_DEDUP_SEC = 15     # Log Ring only once per call (up to 15 seconds)
ANSWER_DEDUP_SEC = 180      # Handle Answer only once per Linkedid (up to 3 minutes)
OPEN_DEDUP_SEC = 180        # Open Ticketum only once per call (up to 3 minutes)

ring_log_seen = {}          # key -> timestamp
answer_seen = {}            # linkedid -> timestamp
ticket_open_seen = {}       # linkedid (or other key) -> timestamp


def _allow_once(cache: dict, key, ttl_sec: int) -> bool:
    """Return True if this key is allowed now (not seen in last ttl_sec)."""
    now = time.time()
    ts = cache.get(key)
    if ts is not None and (now - ts) < ttl_sec:
        return False
    cache[key] = now
    return True


def _cleanup_cache(cache: dict, ttl_sec: int) -> None:
    """Remove old keys to prevent memory growth."""
    now = time.time()
    for k, ts in list(cache.items()):
        if (now - ts) > ttl_sec:
            cache.pop(k, None)


def load_or_create_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                print("✅ Settings file found. Loading configuration...")
                return json.load(f)
        except Exception as e:
            print(f"❌ Error reading settings file: {e}")
            sys.exit(1)
    else:
        print("⚠️  Settings file not found.")
        print("Please enter the server details below:")
        config = {}
        config['host'] = input("Server IP: ").strip()
        config['port'] = 5038
        config['username'] = input("AMI Username: ").strip()
        config['secret'] = input("AMI Password: ").strip()
        config['extension'] = input("Your Extension: ").strip()
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)
        return config


def open_ticketum(caller_id: str, ext_num: str, call_key: str = None) -> bool:
    """
    Open Ticketum only once per call_key (usually linkedid).
    """
    key = call_key or f"{caller_id}->{ext_num}"

    # Stop opening Ticketum repeatedly for the same call
    if not _allow_once(ticket_open_seen, key, OPEN_DEDUP_SEC):
        return False

    url = f"https://ticketum.bki.ir/#/usersummary/{VOIP_ID}/{NID}/{DEPT_ID}/{caller_id}/{ext_num}"

    try:
        webbrowser.open(url)
        print(f"📞 ANSWERED! Caller: {caller_id} -> Opening Ticketum for ext {ext_num} ...")
        return True
    except Exception as e:
        print(f"❌ Error opening browser: {e}")
        return False


def _ext_in_str(ext: str, s: str) -> bool:
    """Match extension as a standalone number (avoid partial matches like 902 in 9020)."""
    s = s or ""
    return bool(re.search(rf'(?<!\d){re.escape(ext)}(?!\d)', s))


def is_call_for_my_ext(event, my_ext: str) -> bool:
    """
    Detect whether this Dial event is related to our extension.
    Priority order: DestCallerIDNum / DestChannel / DialString / fallback to Local/ in Channel.
    """
    dest_chan = event.get('DestChannel', '') or ''
    dial_str = event.get('DialString', '') or ''
    dest_cid = event.get('DestCallerIDNum', '') or ''
    chan = event.get('Channel', '') or ''

    if dest_cid == my_ext:
        return True
    if _ext_in_str(my_ext, dest_chan):
        return True
    if _ext_in_str(my_ext, dial_str):
        return True

    # Fallback for cases like Local/xxx-xxxx;2 or similar
    if chan.startswith("Local/") and _ext_in_str(my_ext, chan):
        return True

    return False


async def main():
    cfg = load_or_create_config()

    manager = Manager(
        loop=asyncio.get_running_loop(),
        host=cfg['host'],
        port=cfg.get('port', 5038),
        username=cfg['username'],
        secret=cfg['secret']
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

    # Store CallerID for each call until it is answered
    # linkedid -> caller_num
    pending_calls = {}
    pending_calls_ts = {}

    def cleanup_pending(ttl_sec=240):
        now = time.time()
        for k, t in list(pending_calls_ts.items()):
            if now - t > ttl_sec:
                pending_calls_ts.pop(k, None)
                pending_calls.pop(k, None)

    @manager.register_event('*')
    async def callback(manager, event):
        my_ext = cfg['extension']

        # Clean up caches to prevent memory growth
        _cleanup_cache(ring_log_seen, RING_LOG_DEDUP_SEC * 4)
        _cleanup_cache(answer_seen, ANSWER_DEDUP_SEC * 2)
        _cleanup_cache(ticket_open_seen, OPEN_DEDUP_SEC * 2)
        cleanup_pending()

        # ---------------------------
        # 1) DialBegin: only store data + log once
        # ---------------------------
        if event.event == 'DialBegin':
            if not is_call_for_my_ext(event, my_ext):
                return

            caller_num = (event.get('CallerIDNum', '') or '').strip()
            linkedid = (event.get('Linkedid', '') or event.get('Uniqueid', '') or '').strip()

            if not linkedid:
                return

            # Caller number must be a real external number
            if caller_num != my_ext and len(caller_num) > 3:
                # Save caller for this linkedid (if empty before, update it)
                if (linkedid not in pending_calls) or (not pending_calls.get(linkedid)):
                    pending_calls[linkedid] = caller_num
                    pending_calls_ts[linkedid] = time.time()

                # Log Ring only once for this call
                ring_key = ("ring", linkedid, my_ext)
                if _allow_once(ring_log_seen, ring_key, RING_LOG_DEDUP_SEC):
                    print(f"🔔 Ringing {my_ext} from {caller_num} (saved, waiting for ANSWER)")

        # ---------------------------
        # 2) DialEnd + ANSWER: open the page only here (once)
        # ---------------------------
        elif event.event == 'DialEnd':
            dial_status = (event.get('DialStatus', '') or '').upper()
            if dial_status != 'ANSWER':
                return

            if not is_call_for_my_ext(event, my_ext):
                return

            linkedid = (event.get('Linkedid', '') or event.get('Uniqueid', '') or '').strip()
            if not linkedid:
                return

            # Prevent duplicate DialEnd ANSWER handling for the same call
            if not _allow_once(answer_seen, linkedid, ANSWER_DEDUP_SEC):
                return

            caller_num = pending_calls.pop(linkedid, (event.get('CallerIDNum', '') or '').strip())
            pending_calls_ts.pop(linkedid, None)

            if caller_num and caller_num != my_ext and len(caller_num) > 3:
                open_ticketum(caller_num, my_ext, call_key=linkedid)

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        manager.close()


if __name__ == '__main__':
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
