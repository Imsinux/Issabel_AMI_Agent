# Ticketum AMI Auto-Opener (Windowless Version)

This application listens to Asterisk AMI events and automatically opens a Ticketum user summary page when a call to a specific extension is answered.

Designed to run:

- As a background Python service
- As a Windows EXE (`--noconsole`)
- Without interactive prompts
- With automatic reconnect and deduplication protection

---

## 🚀 Features

- Listens to Asterisk AMI (`DialBegin` and `DialEnd`)
- Opens Ticketum automatically on call answer
- Prevents duplicate browser openings
- Works without console input
- Logs everything to `app.log`
- Auto reconnects to AMI
- Handles internal/external call filtering
- Compatible with PyInstaller (`--noconsole`)

---

## 📁 Project Structure

```
ticketumapp-V2.5.py
settings.json
app.log
```

- `ticketumapp-V2.5.py` → Main application
- `settings.json` → AMI configuration
- `app.log` → Runtime logs

---

## ⚙️ Requirements

- Python 3.9+
- Asterisk with AMI enabled
- Python package:

```
pip install panoramisk
```

---

## 🛠 Configuration

On first run, if `settings.json` does not exist, a template will be created.

Edit `settings.json` and fill in:

```json
{
  "host": "ASTERISK_IP",
  "port": 5038,
  "username": "AMI_USERNAME",
  "secret": "AMI_PASSWORD",
  "extension": "101"
}
```

### Required Fields

| Field       | Description |
|------------|------------|
| host       | Asterisk server IP |
| port       | AMI port (default 5038) |
| username   | AMI username |
| secret     | AMI password |
| extension  | Extension to monitor |

---

## ▶️ Running the App

### Linux / macOS

```
python3 ticketumapp-V2.5.py
```

### Windows

```
python ticketumapp-V2.5.py
```

---

## 🏗 Building as Windows EXE

To build a background EXE:

```
pyinstaller --noconsole --onefile ticketumapp-V2.5.py
```

The generated EXE will:

- Run without a terminal window
- Create `settings.json` and `app.log` next to the EXE

---

## 🌐 Ticketum URL Format

When a call is answered, the app opens:

```
https://ticketum.bki.ir/#/usersummary/{call_id}/{DEPT_ID}/{caller_id}/{extension}
```

> Note: NID parameter has been removed in this version.

---

## 🔄 How It Works

1. Connects to Asterisk AMI
2. Listens for:
   - `DialBegin`
   - `DialEnd`
3. Stores caller info temporarily
4. When call status = `ANSWER`
5. Opens Ticketum in default browser
6. Prevents duplicate triggers using TTL-based memory cache

---

## 🧠 Duplicate Protection

The app prevents:

- Multiple ring logs
- Multiple answer triggers
- Multiple browser openings

Using time-based deduplication settings:

- `RING_LOG_DEDUP_SEC`
- `ANSWER_DEDUP_SEC`
- `OPEN_DEDUP_SEC`

---

## 📝 Logs

All activity is written to:

```
app.log
```

Example log entries:

```
App started
Connected to AMI successfully
RING: Ext=101 From=09123456789
ANSWERED: Caller=09123456789 Ext=101 -> Ticketum opened
```

---

## 🔐 AMI Requirements

Ensure your `manager.conf` in Asterisk allows:

```
[amiuser]
secret = amipassword
read = call
write = none
```

And AMI is enabled:

```
enabled = yes
port = 5038
```

---

## 🛑 Troubleshooting

### "Invalid config" Error

Make sure all required fields in `settings.json` are filled and not empty.

### No Browser Opens

- Check `app.log`
- Verify AMI events are received
- Confirm extension number matches exactly

### Connection Fails

- Verify Asterisk IP
- Verify AMI credentials
- Check firewall rules

---

## 📌 Notes

- Works best when `CDR_ID_SOURCE = "linkedid"`
- Internal calls can be disabled via:
  ```
  INCLUDE_INTERNAL_CALLS = False
  ```

---

## 📄 License

Internal use only.
Eric Atomicmail.
