import os
import sys
import subprocess
import asyncio
import shutil
import nodriver as uc

def find_browsers():
    """Find all valid Chromium-based browser binaries on the host."""
    found = []
    
    is_windows = sys.platform.startswith('win')
    
    # Define browsers and their common names/paths
    browser_definitions = [
        {"name": "Brave", "binaries": ["brave-browser", "brave", "brave.exe"], "paths": [
            r'%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe',
            r'%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe',
            r'%PROGRAMFILES(X86)%\BraveSoftware\Brave-Browser\Application\brave.exe'
        ]},
        {"name": "Google Chrome", "binaries": ["google-chrome-stable", "google-chrome", "chrome.exe"], "paths": [
            r'%PROGRAMFILES%\Google\Chrome\Application\chrome.exe',
            r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe',
            r'%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe'
        ]},
        {"name": "Microsoft Edge", "binaries": ["microsoft-edge-stable", "microsoft-edge", "msedge.exe"], "paths": [
            r'%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe',
            r'%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe'
        ]},
        {"name": "Vivaldi", "binaries": ["vivaldi-stable", "vivaldi", "vivaldi.exe"], "paths": [
            r'%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe',
            r'%PROGRAMFILES%\Vivaldi\Application\vivaldi.exe'
        ]},
        {"name": "Opera", "binaries": ["opera", "opera.exe"], "paths": [
            r'%PROGRAMFILES%\Opera\launcher.exe',
            r'%LOCALAPPDATA%\Programs\Opera\launcher.exe'
        ]},
        {"name": "Chromium", "binaries": ["chromium-browser", "chromium", "chromium.exe"], "paths": [
            r'%PROGRAMFILES%\Chromium\Application\chrome.exe'
        ]}
    ]

    for browser in browser_definitions:
        # Check PATH first
        for b in browser.binaries:
            path = shutil.which(b)
            if path:
                found.append({"name": browser["name"], "path": path})
                break
        else:
            # Check absolute paths if on Windows
            if is_windows:
                for p in browser["paths"]:
                    expanded = os.path.expandvars(p)
                    if os.path.exists(expanded):
                        found.append({"name": browser["name"], "path": expanded})
                        break
                        
    return found

def run_shell(cmd):
    subprocess.run(cmd, shell=True)

def load_env():
    """Load .env file from the repo root."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir = os.path.dirname(script_dir)
    env_path = os.path.join(repo_dir, '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key] = value.strip()
    return repo_dir

async def warm_profile():
    repo_dir = load_env()
    profile = os.path.join(repo_dir, 'data', 'brave_profile')
    
    print(f"[*] Target Profile: {profile}")

    # Manual override check
    browser_path = os.environ.get('BROWSER_PATH')
    
    if not browser_path:
        browsers = find_browsers()
        
        if not browsers:
            print("\n[!] ERROR: No Chromium-based browser detected on your host system.")
            print("[*] The warmup procedure requires a Chromium-based browser (Brave, Chrome, Edge, etc.)")
            print("[*] Please install one, or set 'BROWSER_PATH' in your environment.")
            sys.exit(1)
            
        if len(browsers) == 1:
            browser_path = browsers[0]["path"]
            print(f"[*] Found {browsers[0]['name']}: {browser_path}")
        else:
            print("\n[*] Multiple Chromium browsers detected. Please choose one for the warmup:")
            for i, b in enumerate(browsers, 1):
                print(f"  {i}) {b['name']} ({b['path']})")
            
            try:
                choice = input("\nEnter number (default 1): ").strip()
                idx = int(choice) - 1 if choice else 0
                if 0 <= idx < len(browsers):
                    browser_path = browsers[idx]["path"]
                else:
                    print("[!] Invalid choice, using option 1.")
                    browser_path = browsers[0]["path"]
            except (ValueError, KeyboardInterrupt):
                print("\n[*] Using default option 1.")
                browser_path = browsers[0]["path"]

    print(f"[*] Starting warmup with: {browser_path}")

    proxy = os.environ.get('HOST_PROXY_URL', '')
    if proxy:
        print(f"[*] Using Proxy: {proxy}")
    else:
        print("[!] Warning: No HOST_PROXY_URL found in .env. Using direct connection.")
    
    print('[*] Stopping proxy container to release lock...')
    run_shell('podman stop sxng-proxy 2>/dev/null || docker stop sxng-proxy 2>/dev/null')
    
    print('[*] Clearing singleton locks...')
    if sys.platform.startswith('win'):
        run_shell(f'del /q "{profile}\\Singleton*" 2>nul')
    else:
        run_shell(f'rm -f {profile}/Singleton*')
    
    print('[*] Launching browser...')
    browser = await uc.start(
        user_data_dir=profile, 
        browser_executable_path=browser_path,
        browser_args=[f'--proxy-server={proxy}'] if proxy else []
    )
    
    print("[*] Opening IP check page...")
    try:
        await browser.get('https://ifconfig.me')
        await asyncio.sleep(2)
    except Exception: pass
    
    print('\n[!] ACTION REQUIRED:')
    print('[!] Solve any CAPTCHAs in the browser window.')
    print('[!] CLOSE the browser window when finished to restart the proxy.')
    
    await browser.get('https://www.google.com/search?q=funny+cats&tbm=vid')

    try:
        while True:
            await asyncio.sleep(1)
            await browser.connection.send(uc.cdp.browser.get_version())
    except Exception: pass

    print('\n[*] Warmup complete. Restarting proxy container...')
    run_shell('podman start sxng-proxy 2>/dev/null || docker start sxng-proxy 2>/dev/null')

if __name__ == '__main__':
    asyncio.run(warm_profile())
