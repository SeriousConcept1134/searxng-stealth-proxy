# Windows Setup Script for SearXNG Stealth Proxy

Write-Host "[*] Creating virtual environment..." -ForegroundColor Cyan
python -m venv venv

Write-Host "[*] Installing dependencies..." -ForegroundColor Cyan
.\venv\Scripts\pip.exe install nodriver fastapi uvicorn

# Fix encoding issue in nodriver/cdp/network.py (common in Python 3.14+)
Write-Host "[*] Applying encoding fix to nodriver..." -ForegroundColor Cyan
$networkFiles = Get-ChildItem -Path .\venv -Filter "network.py" -Recurse | Where-Object { $_.FullName -like "*nodriver\cdp*" }

foreach ($file in $networkFiles) {
    $content = Get-Content -Path $file.FullName -Raw -Encoding Byte
    # Remove the invalid \xb1 character (177 in decimal)
    $newContent = $content | Where-Object { $_ -ne 177 }
    Set-Content -Path $file.FullName -Value $newContent -Encoding Byte
}

Write-Host "`n[+] Setup complete! Use .\venv\Scripts\python.exe scripts\manage.py to warm profile." -ForegroundColor Green
