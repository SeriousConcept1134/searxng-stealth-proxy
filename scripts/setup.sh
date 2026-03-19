#!/bin/bash
python3 -m venv venv
./venv/bin/pip install nodriver fastapi uvicorn

# Fix encoding issue in nodriver/cdp/network.py (common in Python 3.14+)
echo "[*] Applying encoding fix to nodriver..."
find ./venv -name "network.py" -path "*/nodriver/cdp/*" -exec sed -i 's/\xb1//g' {} + 2>/dev/null

echo 'Setup complete. Use ./venv/bin/python scripts/manage.py to warm profile.'
