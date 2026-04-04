import requests, sys, json

TOKEN = "YOUR_BOT_TOKEN"
CHAT_ID = "YOUR_CHAT_ID"
data = json.loads(sys.stdin.read())
msg = f"✅ Claude Code finished a task in session `{data.get('session_id','?')}`"
requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
              json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})