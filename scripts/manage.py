import os
import sys
import subprocess
import asyncio
import shutil
import nodriver as uc

def find_browser():
    """Find a valid browser binary on the host."""
    # Allow user override via env var
    if os.environ.get('BROWSER_PATH'):
        return os.environ.get('BROWSER_PATH')

    # List of common binary names
    binaries = [
        'brave-browser', 'brave', 
        'google-chrome-stable', 'google-chrome', 
        'chromium-browser', 'chromium'
    ]
    for name in binaries:
        path = shutil.which(name)
        if path:
            return path
    return None

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
    # Load environment variables first!
    repo_dir = load_env()
    profile = os.path.join(repo_dir, 'data', 'brave_profile')
    
    print(f"[*] Target Profile: {profile}")

    # Auto-detect browser
    browser_path = find_browser()
    if not browser_path:
        print("[!] Error: Could not find a valid browser (Brave, Chrome, or Chromium).")
        sys.exit(1)
    
    print(f"[*] Found browser: {browser_path}")

    # Use the HOST_PROXY_URL if set, so we solve CAPTCHAs with the same IP
    proxy = os.environ.get('HOST_PROXY_URL', '')
    if proxy:
        print(f"[*] Using Proxy: {proxy}")
    else:
        print("[!] Warning: No HOST_PROXY_URL found in .env. Using direct connection.")
    
    print('[*] Stopping proxy container to release lock...')
    run_shell('podman stop sxng-proxy 2>/dev/null || docker stop sxng-proxy 2>/dev/null')
    
    print('[*] Clearing singleton locks...')
    run_shell(f'rm -f {profile}/Singleton*')
    
    print('[*] Launching browser...')
    browser = await uc.start(
        user_data_dir=profile, 
        browser_executable_path=browser_path,
        browser_args=[f'--proxy-server={proxy}'] if proxy else []
    )
    
    # 1. IP Check
    print("[*] Opening IP check page...")
    page = await browser.get('https://ifconfig.me')
    await asyncio.sleep(2)
    
    # 2. Google Search
    print('[!] Opening Google. Solve CAPTCHAs and close the browser window when done.')
    await browser.get('https://www.google.com/search?q=funny+cats&tbm=vid')

    try:
        while True:
            await asyncio.sleep(1)
            await browser.connection.send(uc.cdp.browser.get_version())
    except Exception: pass

    print('[*] Browser closed. Restarting proxy container...')
    run_shell('podman start sxng-proxy 2>/dev/null || docker start sxng-proxy 2>/dev/null')

if __name__ == '__main__':
    asyncio.run(warm_profile())
