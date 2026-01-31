import asyncio
import webbrowser
import json
import os
import sys
import time
from panoramisk import Manager

# Configuration file name
CONFIG_FILE = 'settings.json'

# Fixed Ticketum Constants
VOIP_ID = "ISBL-901"
NID = "0"
DEPT_ID = "5"

# متغیری برای جلوگیری از باز شدن تکراری تب‌ها در یک ثانیه
last_popup_time = 0

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

def open_ticketum(caller_id, ext_num):
    global last_popup_time
    current_time = time.time()
    
    # اگر کمتر از 3 ثانیه از پاپ‌آپ قبلی گذشته، دوباره باز نکن (برای جلوگیری از تکرار)
    if current_time - last_popup_time < 3:
        return

    print(f"🚀 MATCH FOUND! Caller: {caller_id} -> Opening Ticketum...")
    
    # ذخیره زمان فعلی
    last_popup_time = current_time
    
    url = f"https://ticketum.bki.ir/#/userSummary/{VOIP_ID}/{ext_num}/{caller_id}/{NID}/{DEPT_ID}"
    
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"❌ Error opening browser: {e}")

async def main():
    cfg = load_or_create_config()
    manager = Manager(loop=asyncio.get_running_loop(),
                      host=cfg['host'], port=cfg['port'],
                      username=cfg['username'], secret=cfg['secret'])

    print(f"⏳ Connecting to {cfg['host']}...")
    try:
        await manager.connect()
        print(f"✅ Connected! Watching for DialBegin events on: {cfg['extension']}")
        print("--------------------------------------------------")
    except Exception as e:
        print(f"❌ Connection Failed: {e}")
        input("Press Enter to exit...")
        return

    @manager.register_event('*')
    async def callback(manager, event):
        # این بار فقط به DialBegin گوش می‌دهیم که حاوی شماره واقعی مشتری است
        if event.event == 'DialBegin':
            
            channel = event.get('Channel', '')
            caller_num = event.get('CallerIDNum', '')
            destination = event.get('DestChannel', '') # گاهی مقصد در این فیلد است
            
            my_ext = cfg['extension']

            # بررسی می‌کنیم آیا تماس مربوط به داخلی ماست؟
            # در لاگ شما: Channel: Local/FMPR-9020...
            # پس چک می‌کنیم آیا شماره داخلی ما (9020) در اسم کانال وجود دارد؟
            if (my_ext in channel) or (my_ext in destination):
                
                # شرط مهم: شماره تماس گیرنده نباید خود ما باشیم
                # (اگر شماره مشتری باشد، قطعاً با شماره داخلی فرق دارد)
                if caller_num != my_ext and len(caller_num) > 3:
                    print(f"📞 Detected Call from {caller_num} to {my_ext}")
                    open_ticketum(caller_num, my_ext)

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