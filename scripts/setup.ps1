# Windows Setup Script for SearXNG Stealth Proxy

Write-Host "[*] Creating virtual environment..." -ForegroundColor Cyan
python -m venv venv

Write-Host "[*] Installing dependencies..." -ForegroundColor Cyan
.\venv\Scripts\pip.exe install nodriver fastapi uvicorn "httpx[socks]" lxml

# Fix encoding issue in nodriver/cdp/network.py (common in Python 3.14+)
Write-Host "[*] Applying encoding fix to nodriver..." -ForegroundColor Cyan
$networkFiles = Get-ChildItem -Path .\venv -Filter "network.py" -Recurse |
    Where-Object { $_.FullName -like "*nodriver\cdp*" }

foreach ($file in $networkFiles) {
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    $cleaned = [byte[]]($bytes | Where-Object { $_ -ne 177 })
    if ($cleaned.Length -gt 0) {
        [System.IO.File]::WriteAllBytes($file.FullName, $cleaned)
        Write-Host "[*] Fixed: $($file.FullName)" -ForegroundColor Green
    } else {
        Write-Host "[*] No fix needed: $($file.FullName)" -ForegroundColor Yellow
    }
}

# Fix missing LoaderId in nodriver/cdp/network.py
Write-Host "[*] Applying LoaderId fix to nodriver..." -ForegroundColor Cyan
$networkFile = Get-ChildItem -Path .\venv -Filter "network.py" -Recurse |
    Where-Object { $_.FullName -like "*nodriver\cdp*" } |
    Select-Object -First 1
if ($networkFile) {
    $patch = "`n`nclass LoaderId(str):`n    @classmethod`n    def from_json(cls, json):`n        return cls(json)`n    def to_json(self):`n        return str(self)`n"
    Add-Content -Path $networkFile.FullName -Value $patch
    Write-Host "[*] LoaderId fix applied." -ForegroundColor Green
}

Write-Host "`n[+] Setup complete! Use .\venv\Scripts\python.exe scripts\manage.py to warm profile." -ForegroundColor Green

