import json
with open("data/web_assets.json") as f:
    data = json.load(f)
targets = ["BCH/USDT","HBAR/USDT","ICP/USDT","XLM/USDT"]
for a in data["assets"]:
    if a["symbol"] in targets:
        print(a["symbol"], "is_tradable=", a.get("is_tradable"), "is_active=", a.get("is_active", "N/A"))
