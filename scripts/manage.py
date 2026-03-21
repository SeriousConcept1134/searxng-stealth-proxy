import os
import sys
import subprocess
import asyncio
import random
import shutil
import nodriver as uc

_WARMUP_MARKER = '.needs_warmup'


def find_browsers():
    """Find all valid Chromium-based browser binaries on the host."""
    found = []

    is_windows = sys.platform.startswith('win')

    browser_definitions = [
        {"name": "Brave", "binaries": ["brave-browser", "brave", "brave.exe"], "paths": [
            r'%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe',
            r'%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe',
            r'%PROGRAMFILES(X86)%\BraveSoftware\Brave-Browser\Application\brave.exe',
        ]},
        {"name": "Google Chrome", "binaries": ["google-chrome-stable", "google-chrome", "chrome.exe"], "paths": [
            r'%PROGRAMFILES%\Google\Chrome\Application\chrome.exe',
            r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe',
            r'%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe',
        ]},
        {"name": "Microsoft Edge", "binaries": ["microsoft-edge-stable", "microsoft-edge", "msedge.exe"], "paths": [
            r'%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe',
            r'%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe',
        ]},
        {"name": "Vivaldi", "binaries": ["vivaldi-stable", "vivaldi", "vivaldi.exe"], "paths": [
            r'%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe',
            r'%PROGRAMFILES%\Vivaldi\Application\vivaldi.exe',
        ]},
        {"name": "Opera", "binaries": ["opera", "opera.exe"], "paths": [
            r'%PROGRAMFILES%\Opera\launcher.exe',
            r'%LOCALAPPDATA%\Programs\Opera\launcher.exe',
        ]},
        {"name": "Chromium", "binaries": ["chromium-browser", "chromium", "chromium.exe"], "paths": [
            r'%PROGRAMFILES%\Chromium\Application\chrome.exe',
        ]},
    ]

    for browser in browser_definitions:
        for b in browser['binaries']:
            path = shutil.which(b)
            if path:
                found.append({"name": browser["name"], "path": path})
                break
        else:
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


def get_profile_pool(repo_dir: str) -> list[dict]:
    """Return all configured profiles as a list of dicts with index and path.

    Defaults to data/brave_profile_0, data/brave_profile_1, data/brave_profile_2
    relative to the repo root. Individual paths can be overridden via
    BRAVE_PROFILE_0/1/2 env vars if a non-standard layout is needed.
    """
    profiles = []
    for i in range(3):
        override = os.environ.get(f'BRAVE_PROFILE_{i}')
        path = override if override else os.path.join(repo_dir, 'data', f'brave_profile_{i}')
        profiles.append({"index": i, "path": path})
    return profiles


def needs_warmup(profile_path: str) -> bool:
    """Return True if the profile has a .needs_warmup marker file."""
    return os.path.exists(os.path.join(profile_path, _WARMUP_MARKER))


def clear_warmup_marker(profile_path: str) -> None:
    """Remove the .needs_warmup marker file from the profile directory."""
    marker = os.path.join(profile_path, _WARMUP_MARKER)
    try:
        if os.path.exists(marker):
            os.remove(marker)
    except Exception as e:
        print(f"[!] Warning: Could not remove warmup marker: {e}")


def select_browser(browser_path_override: str | None = None) -> str:
    """Detect available browsers and return the path to use."""
    if browser_path_override:
        return browser_path_override

    browsers = find_browsers()

    if not browsers:
        print("\n[!] ERROR: No Chromium-based browser detected on your host system.")
        print("[*] Please install one, or set 'BROWSER_PATH' in your environment.")
        sys.exit(1)

    if len(browsers) == 1:
        print(f"[*] Found {browsers[0]['name']}: {browsers[0]['path']}")
        return browsers[0]["path"]

    print("\n[*] Multiple Chromium browsers detected. Please choose one for the warmup:")
    for i, b in enumerate(browsers, 1):
        print(f"  {i}) {b['name']} ({b['path']})")

    try:
        choice = input("\nEnter number (default 1): ").strip()
        idx = int(choice) - 1 if choice else 0
        if 0 <= idx < len(browsers):
            return browsers[idx]["path"]
        print("[!] Invalid choice, using option 1.")
        return browsers[0]["path"]
    except (ValueError, KeyboardInterrupt, EOFError):
        print("\n[*] Using default option 1.")
        return browsers[0]["path"]


async def run_warmup(profile: dict, browser_path: str, proxy: str, ua: str,
                     is_recovery: bool = False) -> None:
    """Run the full warmup sequence for a single profile.

    When is_recovery is True (profile was CAPTCHA-flagged), seed queries
    are skipped and the user is taken straight to the CAPTCHA prompt,
    since the blocked session will reject all requests anyway.
    When is_recovery is False (fresh profile setup), seed queries run
    first to build session history before handing off to the user.
    """
    profile_path = profile["path"]
    idx = profile["index"]

    print(f"\n[*] Warming up profile {idx}: {profile_path}")

    print('[*] Stopping proxy container to release lock...')
    run_shell('podman stop sxng-proxy 2>/dev/null || docker stop sxng-proxy 2>/dev/null')

    print('[*] Waiting for filesystem to settle...')
    await asyncio.sleep(2)

    print('[*] Clearing singleton locks...')
    if sys.platform.startswith('win'):
        run_shell(f'del /q "{profile_path}\\Singleton*" 2>nul')
    else:
        run_shell(f'rm -f {profile_path}/Singleton*')

    print('[*] Launching browser...')
    args = [
        '--no-first-run',
        '--no-default-browser-check',
        '--password-store=basic',
        '--window-size=1920,1080',
        f'--user-agent={ua}',
    ]
    if proxy:
        args.append(f'--proxy-server={proxy}')
        args.append('--proxy-bypass-list=<-loopback>')

    browser = None
    try:
        browser = await uc.start(
            user_data_dir=profile_path,
            browser_executable_path=browser_path,
            browser_args=args
        )
    except Exception as e:
        print(f"\n[!] Critical Error: Could not connect to browser: {e}")
        print("[*] Troubleshooting steps:")
        print("  1. Ensure all instances of the chosen browser are closed.")
        print("  2. If problem persists, try a different browser.")
        print("  3. Check if your antivirus/firewall is blocking local websocket connections.")
        return

    try:
        print("[*] Opening IP check page...")
        try:
            await browser.get('https://ifconfig.me')
            await asyncio.sleep(2)
        except Exception:
            pass

        if is_recovery:
            print("[*] Recovery warmup — skipping seed queries, proceeding to CAPTCHA prompt.")
        else:
            print("[*] Running seed queries to build session history...")
            seed_queries = [
                "https://www.google.com/search?q=weather+forecast+this+week",
                "https://www.google.com/search?q=best+pasta+carbonara+recipe",
                "https://news.google.com",
                "https://www.google.com/maps",
                "https://www.google.com/search?q=how+to+learn+guitar",
                "https://www.google.com/search?q=funny+cats&tbm=vid",
            ]
            for i, seed_url in enumerate(seed_queries, 1):
                try:
                    print(f"[*] Seed query {i}/{len(seed_queries)}: {seed_url}")
                    await browser.get(seed_url)
                    await asyncio.sleep(random.uniform(3.0, 6.0))
                    await browser.evaluate("window.scrollBy(0, 350)")
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                except Exception:
                    pass

        print('\n[!] ACTION REQUIRED:')
        print('[!] Solve any CAPTCHAs in the browser window.')
        print('[!] CLOSE the browser window when finished.')

        try:
            await browser.get('https://www.google.com/search?q=funny+cats&tbm=vid')
        except Exception:
            pass

        try:
            while True:
                await asyncio.sleep(1)
                if browser._process and browser._process.returncode is not None:
                    break
                await browser.connection.send(uc.cdp.browser.get_version())
        except (Exception, asyncio.CancelledError):
            pass

    finally:
        if browser:
            print("[*] Shutting down browser interface...")
            try:
                await browser.stop()
            except Exception:
                pass

    clear_warmup_marker(profile_path)
    print(f"[*] Warmup complete for profile {idx}. Marker cleared.")


async def main():
    repo_dir = load_env()
    all_profiles = get_profile_pool(repo_dir)

    ua_file = os.path.join(repo_dir, 'patches', 'gsa_useragents.txt')
    ua = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.7680.153 Safari/537.36"
    )
    if os.path.exists(ua_file):
        try:
            with open(ua_file, 'r') as f:
                content = f.read().strip()
                if content:
                    ua = content
        except Exception as e:
            print(f"[!] Warning: Could not read {ua_file}: {e}")

    print(f"[*] UA: {ua}")

    proxy = os.environ.get('HOST_PROXY_URL', '')
    if proxy:
        print(f"[*] Using Proxy: {proxy}")
    else:
        print("[!] Warning: No HOST_PROXY_URL found in .env. Using direct connection.")

    browser_path = select_browser(os.environ.get('BROWSER_PATH'))
    print(f"[*] Using browser: {browser_path}")

    # Determine which profiles need warmup
    flagged = [p for p in all_profiles if needs_warmup(p["path"])]
    unflagged = [p for p in all_profiles if not needs_warmup(p["path"])]

    if flagged:
        # One or more profiles flagged — work through them
        queue = list(flagged)
        print(f"\n[*] {len(queue)} profile(s) require warmup: "
              f"{[p['index'] for p in queue]}")

        while queue:
            profile = queue.pop(0)
            await run_warmup(profile, browser_path, proxy, ua, is_recovery=True)

            if queue:
                print(f"\n[*] {len(queue)} profile(s) still need warmup: "
                      f"{[p['index'] for p in queue]}")
                try:
                    answer = input("[?] Warm up next profile? [Y/n]: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    answer = 'n'

                if answer in ('n', 'no'):
                    print("[*] Exiting. Remaining profiles will be warmed on next run.")
                    break
    else:
        # No profiles flagged — ask which one the user wants to warm anyway
        print("\n[*] No profiles currently marked for warmup.")
        print("[*] Available profiles:")
        for p in all_profiles:
            print(f"  {p['index']}) {p['path']}")

        try:
            choice = input(
                f"\nEnter profile index to warm up "
                f"(0-{len(all_profiles) - 1}, default 0): "
            ).strip()
            idx = int(choice) if choice else 0
            matched = [p for p in all_profiles if p["index"] == idx]
            if not matched:
                print(f"[!] Invalid index, using profile 0.")
                matched = [all_profiles[0]]
            profile = matched[0]
        except (ValueError, KeyboardInterrupt, EOFError):
            print("\n[*] Using profile 0.")
            profile = all_profiles[0]

        await run_warmup(profile, browser_path, proxy, ua)

    print('\n[*] Restarting proxy container...')
    run_shell('podman start sxng-proxy 2>/dev/null || docker start sxng-proxy 2>/dev/null')


if __name__ == '__main__':
    asyncio.run(main())
