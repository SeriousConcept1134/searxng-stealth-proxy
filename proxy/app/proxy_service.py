from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
import nodriver as uc
import asyncio
import os
import time
import random
import logging
from lxml import html

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sxng-proxy")

app = FastAPI(title="SearXNG Stealth Proxy")

UA_FILE = '/app/patches/gsa_useragents.txt'
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.7680.153 Safari/537.36"
)
FATAL_BROWSER_ERRORS = (ConnectionRefusedError, ProcessLookupError, BrokenPipeError)
_TIMEZONE_FALLBACK = 'America/New_York'
_WARMUP_MARKER = '.needs_warmup'

# Search mode: 'direct' uses the raw Google URL, 'humanized' simulates
# organic search via the Google homepage. Direct is the default.
_SEARCH_MODE = os.environ.get('SEARCH_MODE', 'direct').lower()

# Browser mode: 'on_demand' starts browsers as needed (default, lower RAM),
# 'concurrent' pre-starts all profile browsers at container launch for
# instant rotation with no startup penalty.
_BROWSER_MODE = os.environ.get('BROWSER_MODE', 'on_demand').lower()

# Serialise requests to avoid concurrent Google hits from the same session
_search_semaphore = asyncio.Semaphore(1)
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 3.5
_MAX_REQUEST_JITTER = 2.5

# Keepalive interval bounds in seconds (18–28 minutes)
_KEEPALIVE_MIN = 18 * 60
_KEEPALIVE_MAX = 28 * 60

# Query pool for humanized keepalive searches.
# Each entry is a (query, tbm) tuple — mix of web and video to build
# cross-property breadth. Sampled randomly each cycle.
_KEEPALIVE_QUERIES = [
    ("best hiking trails near mountains", ""),
    ("how to make sourdough bread at home", ""),
    ("latest space exploration news", ""),
    ("funny cat compilations", "vid"),
    ("learn python programming beginners", ""),
    ("best documentary films", "vid"),
    ("home garden tips spring planting", ""),
    ("world travel destinations bucket list", ""),
    ("easy pasta recipes dinner", ""),
    ("northern lights photography tips", ""),
    ("electric cars comparison review", ""),
    ("jazz music relaxing playlist", "vid"),
    ("how does the stock market work", ""),
    ("best science podcasts 2024", ""),
    ("ocean wildlife documentary", "vid"),
    ("beginner yoga morning routine", "vid"),
    ("history of ancient rome", ""),
    ("coffee brewing methods guide", ""),
    ("wildlife photography tips nature", ""),
    ("classic movies everyone should watch", ""),
]

# Navigator overrides injected before any page script runs
_NAVIGATOR_OVERRIDES = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
"""

# Detected egress timezone — populated at browser startup
_egress_timezone: str = _TIMEZONE_FALLBACK

# --- Profile rotation pool ---
_PROFILES: list[str] = []
_active_profile_idx: int = 0
_profile_flagged: dict[int, bool] = {}
_browser: uc.Browser | None = None           # on_demand mode
_browsers: dict[int, uc.Browser | None] = {} # concurrent mode

# Per-profile locks prevent keepalive and get_browser() from starting
# concurrent browser instances on the same profile directory.
# Only used in on_demand mode — concurrent mode owns each profile exclusively.
_profile_locks: dict[int, asyncio.Lock] = {}


def _init_profile_pool() -> None:
    """Populate the profile pool from environment variables.

    Reads BRAVE_PROFILE_0, BRAVE_PROFILE_1, BRAVE_PROFILE_2 and falls
    back to the legacy BRAVE_PROFILE single-profile variable so existing
    setups are not broken.
    """
    global _PROFILES, _profile_flagged

    profiles = []
    for i in range(3):
        p = os.environ.get(f'BRAVE_PROFILE_{i}')
        if p:
            profiles.append(p)

    if not profiles:
        fallback = os.environ.get('BRAVE_PROFILE', '/data/brave_profile')
        profiles = [fallback]

    _PROFILES = profiles
    _profile_flagged = {i: False for i in range(len(_PROFILES))}
    _profile_locks = {i: asyncio.Lock() for i in range(len(_PROFILES))}
    logger.info(f"Profile pool initialised: {_PROFILES}")


def _get_next_healthy_profile() -> tuple[str, int]:
    """Return the next unflagged profile path and its index.

    Rotates through the pool starting from the current active index.
    If all profiles are flagged, returns the first profile with a warning.
    """
    for offset in range(len(_PROFILES)):
        idx = (_active_profile_idx + offset) % len(_PROFILES)
        if not _profile_flagged[idx]:
            return _PROFILES[idx], idx
    logger.warning("All profiles flagged — using profile 0 anyway, re-warm required")
    return _PROFILES[0], 0


def _write_warmup_marker(profile_path: str) -> None:
    """Write a .needs_warmup marker file into the profile directory."""
    try:
        marker = os.path.join(profile_path, _WARMUP_MARKER)
        with open(marker, 'w') as f:
            f.write(str(time.time()))
    except Exception as e:
        logger.warning(f"Could not write warmup marker for {profile_path}: {e}")


def _flag_active_profile() -> None:
    """Flag the current active profile and write the warmup marker file."""
    _profile_flagged[_active_profile_idx] = True
    profile_path = _PROFILES[_active_profile_idx]
    logger.warning(
        f"Profile {_active_profile_idx} ({profile_path}) flagged — re-warm required"
    )
    _write_warmup_marker(profile_path)


def load_ua() -> str:
    try:
        with open(UA_FILE, 'r') as f:
            content = f.read().strip()
            if content:
                return content
    except Exception:
        pass
    return DEFAULT_UA


def is_bot_detected(url: str) -> bool:
    return "/sorry/" in url or "sorry.google.com" in url


async def detect_egress_timezone(proxy_url: str) -> str:
    """Detect the IANA timezone of the current WARP egress IP."""
    import httpx
    try:
        transport = httpx.AsyncHTTPTransport(proxy=proxy_url) if proxy_url else None
        async with httpx.AsyncClient(transport=transport, timeout=5) as client:
            resp = await client.get("http://ip-api.com/json?fields=timezone")
            tz = resp.json().get("timezone", _TIMEZONE_FALLBACK)
            logger.info(f"Detected egress timezone: {tz}")
            return tz
    except Exception as e:
        logger.warning(f"Timezone detection failed, using fallback '{_TIMEZONE_FALLBACK}': {e}")
        return _TIMEZONE_FALLBACK


async def move_to_element(page, element) -> None:
    """Simulate cursor movement toward the element before clicking."""
    try:
        await page.evaluate("""
            (function() {
                const el = document.querySelector('textarea[name="q"], input[name="q"]');
                if (!el) return;
                el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true, cancelable: true}));
                el.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, cancelable: true}));
            })()
        """)
        await asyncio.sleep(random.uniform(0.1, 0.3))
    except Exception:
        pass


async def type_humanlike(page, text: str) -> None:
    """Type text character by character via CDP key events."""
    for char in text:
        await page.send(uc.cdp.input_.dispatch_key_event(
            type_='keyDown', text=char
        ))
        await asyncio.sleep(random.uniform(0.04, 0.16))
        await page.send(uc.cdp.input_.dispatch_key_event(
            type_='keyUp', text=char
        ))
        await asyncio.sleep(random.uniform(0.02, 0.08))


async def submit_search(page, search_input, query_text: str) -> bool:
    """Simulate cursor movement, focus the input, type the query, and submit."""
    await move_to_element(page, search_input)
    await search_input.click()
    await asyncio.sleep(random.uniform(0.2, 0.5))

    await type_humanlike(page, query_text)
    await asyncio.sleep(0.1)

    await page.send(uc.cdp.input_.dispatch_key_event(
        type_='keyDown', windows_virtual_key_code=13, native_virtual_key_code=13
    ))

    for _ in range(30):
        await asyncio.sleep(0.2)
        if "/search" in page.url and "q=" in page.url:
            return True

    logger.warning("Enter key did not trigger navigation, trying form submit fallback")
    await page.evaluate(
        "document.querySelector('form[action=\"/search\"]')?.submit()"
    )
    for _ in range(20):
        await asyncio.sleep(0.2)
        if "/search" in page.url and "q=" in page.url:
            return True

    return False


def inject_params(validated_url: str, start_val: str, safe_val: str) -> str:
    """Append missing start and safe params to the validated URL string."""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

    parsed = urlparse(validated_url)
    params = parse_qs(parsed.query)

    changed = False
    if start_val != '0' and params.get('start', ['0'])[0] != start_val:
        params['start'] = [start_val]
        changed = True
    if safe_val and params.get('safe', [''])[0] != safe_val:
        params['safe'] = [safe_val]
        changed = True

    if not changed:
        return validated_url

    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.error(f"VALIDATION ERROR: {exc.errors()}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


async def get_browser() -> uc.Browser:
    """Return the active browser, starting a new one if needed.

    In on_demand mode: starts a browser on first call, reuses on subsequent.
    In concurrent mode: all browsers are pre-started at startup; returns the
    browser for the current active profile directly.
    """
    global _browser, _active_profile_idx, _egress_timezone

    if _BROWSER_MODE == 'concurrent':
        b = _browsers.get(_active_profile_idx)
        if b is None:
            # Shouldn't happen normally — concurrent browsers are started at
            # startup. Recover gracefully by starting one now.
            b = await _start_browser_for_profile(_active_profile_idx)
            _browsers[_active_profile_idx] = b
            _egress_timezone = await detect_egress_timezone(
                os.environ.get('PROXY_URL', '')
            )
        return b

    # on_demand mode
    if not _browser:
        if not _PROFILES:
            _init_profile_pool()

        profile, idx = _get_next_healthy_profile()
        _active_profile_idx = idx

        async with _profile_locks.get(idx, asyncio.Lock()):
            _browser = await _start_browser_for_profile(idx)

        _egress_timezone = await detect_egress_timezone(
            os.environ.get('PROXY_URL', '')
        )

    return _browser


async def _reset_browser() -> None:
    """Stop the current browser and clear the global reference.

    In concurrent mode, stops and clears only the active profile's browser.
    """
    global _browser
    if _BROWSER_MODE == 'concurrent':
        b = _browsers.get(_active_profile_idx)
        if b:
            try:
                await b.stop()
            except Exception:
                pass
        _browsers[_active_profile_idx] = None
    else:
        if _browser:
            try:
                await _browser.stop()
            except Exception:
                pass
        _browser = None


async def _rotate_profile() -> None:
    """Flag the active profile, write the warmup marker, and reset the browser."""
    _flag_active_profile()
    if _BROWSER_MODE == 'concurrent':
        # In concurrent mode we don't stop the browser — just update the
        # active index so the next search routes to a healthy profile.
        global _active_profile_idx
        _, idx = _get_next_healthy_profile()
        _active_profile_idx = idx
    else:
        await _reset_browser()


async def _start_browser_for_profile(idx: int) -> uc.Browser:
    """Start a new browser instance for the given profile index.

    Clears stale SingletonLocks, applies a brief OS release delay, and
    returns the started browser. Used by both on_demand and concurrent modes.
    """
    import glob
    profile = _PROFILES[idx]
    proxy = os.environ.get('PROXY_URL', '')

    args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--window-size=1920,1080",
        "--password-store=basic",
        "--disable-gpu" if os.name != 'nt' else "--enable-gpu",
    ]
    if proxy:
        args.append(f'--proxy-server={proxy}')

    logger.info(f"Initializing browser with profile {idx} ({profile})")

    for lock_file in glob.glob(os.path.join(profile, 'Singleton*')):
        try:
            os.remove(lock_file)
        except Exception:
            pass

    await asyncio.sleep(2.0)
    return await uc.start(
        user_data_dir=profile,
        browser_executable_path='/usr/bin/brave-browser-stable',
        headless=True,
        browser_args=args
    )


async def _timezone_check_loop() -> None:
    """Periodically re-detect the WARP egress timezone and update the cache.

    Runs every 30 minutes. If the timezone has changed (e.g. WARP re-routed
    to a different egress), updates _egress_timezone so subsequent tabs pick
    it up via set_timezone_override without needing a browser restart.
    """
    global _egress_timezone
    while True:
        await asyncio.sleep(30 * 60)
        proxy = os.environ.get('PROXY_URL', '')
        new_tz = await detect_egress_timezone(proxy)
        if new_tz != _egress_timezone:
            logger.info(
                f"Egress timezone changed: {_egress_timezone} → {new_tz}, updating"
            )
            _egress_timezone = new_tz


async def _humanized_keepalive_search(browser: uc.Browser, query: str, tbm: str) -> bool:
    """Perform a single humanized search on the given browser for keepalive purposes.

    Navigates to the Google homepage, types the query, submits, and waits
    briefly for results. Returns True if no bot detection was triggered,
    False otherwise. Does not raise — all exceptions are caught internally.
    """
    page = None
    try:
        page = await browser.get(new_tab=True)

        import nodriver.cdp.network as network
        import nodriver.cdp.page as cdp_page
        import nodriver.cdp.emulation as emulation

        target_ua = load_ua()
        await page.send(network.set_user_agent_override(
            user_agent=target_ua,
            accept_language="en-US,en;q=0.9",
            platform="Linux",
        ))
        await page.send(network.set_extra_http_headers(headers=network.Headers({
            "Sec-CH-UA": '"Chromium";v="146", "Brave";v="146", "Not/A)Brand";v="99"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Linux"',
            "Referer": "https://www.google.com/",
        })))
        await page.send(cdp_page.add_script_to_evaluate_on_new_document(
            source=_NAVIGATOR_OVERRIDES
        ))
        await page.send(emulation.set_timezone_override(timezone_id=_egress_timezone))
        await page.send(emulation.set_device_metrics_override(
            width=1920, height=1080, device_scale_factor=1, mobile=False,
        ))

        entry_url = "https://www.google.com/webhp?hl=en"
        if tbm == 'vid':
            entry_url += "&tbm=vid"

        await page.get(entry_url)

        search_input = await page.select('textarea[name="q"], input[name="q"]', timeout=5)
        if not search_input:
            return False

        await submit_search(page, search_input, query)

        if is_bot_detected(page.url):
            return False

        # Brief dwell to simulate reading results
        await asyncio.sleep(random.uniform(3.0, 6.0))
        await page.evaluate("window.scrollBy(0, 350)")
        await asyncio.sleep(random.uniform(1.0, 2.5))

        return True

    except Exception as e:
        logger.debug(f"Humanized keepalive search error: {e}")
        return False
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


async def _keepalive_loop(profile_idx: int) -> None:
    """Background coroutine that periodically runs humanized searches to keep
    the given profile's session trust score alive.

    For the active profile (browser already running), opens new tabs directly
    without holding the search semaphore — concurrent real searches proceed
    in their own tabs unblocked. For inactive profiles, spins up a temporary
    browser, runs the searches, and shuts it down cleanly.

    Flagged profiles run an automated recovery sequence instead: one humanized
    search as a probe — if it passes, two more are run to build session depth,
    then the flag is cleared. If the probe fails, the profile stays flagged.
    """
    # Stagger startup so profiles don't all fire at once
    stagger = random.uniform(60, 180) * (profile_idx + 1)
    await asyncio.sleep(stagger)

    while True:
        interval = random.uniform(_KEEPALIVE_MIN, _KEEPALIVE_MAX)

        if _profile_flagged.get(profile_idx, False):
            profile_path = _PROFILES[profile_idx] if profile_idx < len(_PROFILES) else None
            if not profile_path:
                await asyncio.sleep(interval)
                continue

            logger.info(f"Recovery check for flagged profile {profile_idx}")

            # In concurrent mode the profile has its own persistent browser —
            # use it directly without spinning up a temporary one.
            if _BROWSER_MODE == 'concurrent':
                b = _browsers.get(profile_idx)
                if b is None:
                    await asyncio.sleep(interval)
                    continue
                try:
                    probe_query, probe_tbm = random.choice(_KEEPALIVE_QUERIES)
                    probe_ok = await _humanized_keepalive_search(b, probe_query, probe_tbm)

                    if not probe_ok:
                        logger.info(
                            f"Recovery check: profile {profile_idx} still blocked"
                        )
                    else:
                        follow_ups = random.sample(
                            [q for q in _KEEPALIVE_QUERIES
                             if q != (probe_query, probe_tbm)],
                            k=min(2, len(_KEEPALIVE_QUERIES) - 1)
                        )
                        for query, tbm in follow_ups:
                            await _humanized_keepalive_search(b, query, tbm)
                            await asyncio.sleep(random.uniform(2.0, 4.0))
                        _profile_flagged[profile_idx] = False
                        marker = os.path.join(profile_path, _WARMUP_MARKER)
                        try:
                            if os.path.exists(marker):
                                os.remove(marker)
                        except Exception as e:
                            logger.warning(f"Could not remove warmup marker: {e}")
                        logger.info(
                            f"Profile {profile_idx} recovered automatically — "
                            f"flag and marker cleared"
                        )
                except Exception as e:
                    logger.warning(f"Recovery check failed for profile {profile_idx}: {e}")
                await asyncio.sleep(interval)
                continue

            # on_demand mode — clear stale locks and spin up a temporary browser.
            import glob
            for lock_file in glob.glob(os.path.join(profile_path, 'Singleton*')):
                try:
                    os.remove(lock_file)
                except Exception:
                    pass

            proxy = os.environ.get('PROXY_URL', '')
            args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1920,1080",
                "--password-store=basic",
                "--disable-gpu",
            ]
            if proxy:
                args.append(f'--proxy-server={proxy}')

            try:
                lock = _profile_locks.get(profile_idx, asyncio.Lock())
                async with lock:
                    tmp_browser = await uc.start(
                        user_data_dir=profile_path,
                        browser_executable_path='/usr/bin/brave-browser-stable',
                        headless=True,
                        browser_args=args
                    )
                    try:
                        # Probe: one humanized search to test if session recovered
                        probe_query, probe_tbm = random.choice(_KEEPALIVE_QUERIES)
                        probe_ok = await _humanized_keepalive_search(
                            tmp_browser, probe_query, probe_tbm
                        )

                        if not probe_ok:
                            logger.info(
                                f"Recovery check: profile {profile_idx} still blocked"
                            )
                        else:
                            # Probe passed — run 2 more searches to build session depth
                            follow_ups = random.sample(
                                [q for q in _KEEPALIVE_QUERIES
                                 if q != (probe_query, probe_tbm)],
                                k=min(2, len(_KEEPALIVE_QUERIES) - 1)
                            )
                            for query, tbm in follow_ups:
                                await _humanized_keepalive_search(
                                    tmp_browser, query, tbm
                                )
                                await asyncio.sleep(random.uniform(2.0, 4.0))

                            # Clear the flag — profile is warm again
                            _profile_flagged[profile_idx] = False
                            marker = os.path.join(profile_path, _WARMUP_MARKER)
                            try:
                                if os.path.exists(marker):
                                    os.remove(marker)
                            except Exception as e:
                                logger.warning(f"Could not remove warmup marker: {e}")
                            logger.info(
                                f"Profile {profile_idx} recovered automatically — "
                                f"flag and marker cleared"
                            )
                    finally:
                        try:
                            await tmp_browser.stop()
                        except Exception:
                            pass
                        await asyncio.sleep(3.0)
            except Exception as e:
                logger.warning(f"Recovery check failed for profile {profile_idx}: {e}")

            await asyncio.sleep(interval)
            continue

        profile_path = _PROFILES[profile_idx] if profile_idx < len(_PROFILES) else None
        if not profile_path:
            await asyncio.sleep(interval)
            continue

        # Pick 2–3 random queries for this keepalive cycle
        n_queries = random.randint(2, 3)
        cycle_queries = random.sample(_KEEPALIVE_QUERIES, k=n_queries)

        try:
            if _BROWSER_MODE == 'concurrent':
                # Concurrent mode — each profile has its own persistent browser.
                # Use it directly without any locking.
                b = _browsers.get(profile_idx)
                if b is None:
                    await asyncio.sleep(interval)
                    continue
                logger.info(
                    f"Keepalive: running {n_queries} humanized searches "
                    f"on profile {profile_idx} (concurrent)"
                )
                for query, tbm in cycle_queries:
                    await _humanized_keepalive_search(b, query, tbm)
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                logger.info(f"Keepalive complete for profile {profile_idx}")
            elif profile_idx == _active_profile_idx and _browser is not None:
                # on_demand active profile — reuse the running browser directly.
                # Do NOT hold _search_semaphore for the full humanized sequence:
                # real search requests open their own tabs concurrently.
                logger.info(
                    f"Keepalive: running {n_queries} humanized searches "
                    f"on active profile {profile_idx}"
                )
                for query, tbm in cycle_queries:
                    await _humanized_keepalive_search(_browser, query, tbm)
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                logger.info(f"Keepalive complete for active profile {profile_idx}")
            else:
                # on_demand inactive profile — spin up a temporary browser.
                lock = _profile_locks.get(profile_idx, asyncio.Lock())
                async with lock:
                    tmp_browser = await _start_browser_for_profile(profile_idx)
                    try:
                        logger.info(
                            f"Keepalive: running {n_queries} humanized searches "
                            f"on inactive profile {profile_idx}"
                        )
                        for query, tbm in cycle_queries:
                            await _humanized_keepalive_search(tmp_browser, query, tbm)
                            await asyncio.sleep(random.uniform(2.0, 4.0))
                        logger.info(
                            f"Keepalive complete for inactive profile {profile_idx}"
                        )
                    finally:
                        try:
                            await tmp_browser.stop()
                        except Exception:
                            pass
                        await asyncio.sleep(3.0)

        except Exception as e:
            logger.warning(f"Keepalive failed for profile {profile_idx}: {e}")

        await asyncio.sleep(interval)


def clean_html(content):
    """Shrink the HTML while keeping result markers AND thumbnail scripts"""
    try:
        dom = html.fromstring(content)
        tags_to_strip = ['style', 'svg', 'noscript', 'header', 'footer', 'iframe', 'canvas']
        for tag_name in tags_to_strip:
            for tag in dom.xpath(f'//{tag_name}'):
                tag.getparent().remove(tag)

        for script in dom.xpath('//script'):
            text = (script.text or "") + (script.tail or "")
            if any(marker in text for marker in ["google.ldi", "google.pim", "dimg_", "_setImagesSrc"]):
                continue
            script.getparent().remove(script)

        return html.tostring(dom, encoding='unicode')
    except Exception as e:
        logger.warning(f"Cleanup failed: {e}")
        return content


@app.on_event("startup")
async def startup_event():
    global _egress_timezone
    _init_profile_pool()
    logger.info(f"Search mode: {_SEARCH_MODE}")
    logger.info(f"Browser mode: {_BROWSER_MODE}")

    proxy = os.environ.get('PROXY_URL', '')

    if _BROWSER_MODE == 'concurrent':
        # Pre-start a browser for every profile in the pool.
        for i in range(len(_PROFILES)):
            try:
                b = await _start_browser_for_profile(i)
                _browsers[i] = b
                logger.info(f"Pre-started browser for profile {i}")
            except Exception as e:
                logger.error(f"Failed to pre-start browser for profile {i}: {e}")
                _browsers[i] = None
        # Detect timezone once — all browsers share the same WARP egress.
        _egress_timezone = await detect_egress_timezone(proxy)
    else:
        # on_demand: timezone detected lazily at first browser start.
        pass

    idle = int(os.environ.get('STARTUP_IDLE_SECONDS', '0'))
    if idle > 0:
        logger.info(f"Startup idle: waiting {idle}s before accepting requests")
        await asyncio.sleep(idle)

    # Start keepalive loops and timezone check loop
    for i in range(len(_PROFILES)):
        asyncio.ensure_future(_keepalive_loop(i))
    asyncio.ensure_future(_timezone_check_loop())
    logger.info(f"Keepalive loops started for {len(_PROFILES)} profile(s)")


@app.get('/search')
async def search(request: Request):
    global _last_request_time

    url = request.query_params.get('url')
    if not url:
        raise HTTPException(status_code=400, detail="Missing url parameter")

    async with _search_semaphore:
        elapsed = time.monotonic() - _last_request_time
        gap = _MIN_REQUEST_GAP + random.uniform(0, _MAX_REQUEST_JITTER)
        if elapsed < gap:
            wait = gap - elapsed
            logger.info(f"Rate limiting: waiting {wait:.2f}s before next request")
            await asyncio.sleep(wait)
        _last_request_time = time.monotonic()

        return await _do_search(url)


async def _do_search(url: str, _tried_profiles: set | None = None) -> HTMLResponse | JSONResponse:
    if _tried_profiles is None:
        _tried_profiles = set()

    # If every profile in the pool is already flagged, return 429 immediately
    # rather than falling through to get_browser() which would re-use a flagged
    # profile and loop indefinitely.
    if _PROFILES and all(_profile_flagged.get(i, False) for i in range(len(_PROFILES))):
        logger.error("All profiles exhausted — returning 429")
        return JSONResponse({"error": "captcha"}, status_code=429)

    start_perf = time.perf_counter()
    b = await get_browser()

    # Record the active profile index after get_browser() has selected it,
    # so rotation correctly tracks which profiles have been attempted.
    _tried_profiles.add(_active_profile_idx)
    page = await b.get(new_tab=True)

    logger.info(
        f"Using profile {_active_profile_idx} ({_PROFILES[_active_profile_idx]})"
    )

    try:
        import nodriver.cdp.network as network
        import nodriver.cdp.page as cdp_page
        import nodriver.cdp.emulation as emulation
        from urllib.parse import urlparse, parse_qs

        target_ua = load_ua()
        await page.send(network.set_user_agent_override(
            user_agent=target_ua,
            accept_language="en-US,en;q=0.9",
            platform="Linux",
        ))
        await page.send(network.set_extra_http_headers(headers=network.Headers({
            "Sec-CH-UA": '"Chromium";v="146", "Brave";v="146", "Not/A)Brand";v="99"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Linux"',
            "Referer": "https://www.google.com/",
        })))
        await page.send(cdp_page.add_script_to_evaluate_on_new_document(
            source=_NAVIGATOR_OVERRIDES
        ))
        await page.send(emulation.set_timezone_override(
            timezone_id=_egress_timezone
        ))
        await page.send(emulation.set_device_metrics_override(
            width=1920,
            height=1080,
            device_scale_factor=1,
            mobile=False,
        ))

        # --- SEARCH FLOW ---
        parsed_incoming = urlparse(url)
        params = parse_qs(parsed_incoming.query)

        query_text = params.get('q', [''])[0]
        start_val = params.get('start', ['0'])[0]
        safe_val = params.get('safe', [''])[0]
        tbm_val = params.get('tbm', [''])[0]
        hl_val = params.get('hl', ['en'])[0]

        if _SEARCH_MODE == 'humanized' and query_text:
            entry_url = f"https://www.google.com/webhp?hl={hl_val}"
            if tbm_val == 'vid':
                entry_url += "&tbm=vid"

            logger.info(f"Humanizing search for: '{query_text}' (tbm={tbm_val})")
            await page.get(entry_url)

            search_input = await page.select('textarea[name="q"], input[name="q"]', timeout=5)
            if not search_input:
                logger.error("Could not find search input field — returning 503")
                return JSONResponse({"error": "input_not_found"}, status_code=503)

            navigated = await submit_search(page, search_input, query_text)
            if not navigated:
                logger.error("Search submission failed to trigger navigation — returning 503")
                return JSONResponse({"error": "navigation_failed"}, status_code=503)

            if is_bot_detected(page.url):
                logger.error("BOT DETECTION on submission — rotating profile")
                await _rotate_profile()
                if len(_tried_profiles) >= len(_PROFILES):
                    logger.error("All profiles exhausted — returning 429")
                    return JSONResponse({"error": "captcha"}, status_code=429)
                logger.info(f"Retrying with next profile (tried: {_tried_profiles})")
                return await _do_search(url, _tried_profiles=_tried_profiles)

            validated_url = page.url
            logger.info(f"Obtained validated URL: {validated_url}")

            final_url = inject_params(validated_url, start_val, safe_val)
            if final_url != validated_url:
                escaped = final_url.replace("'", "\\'")
                await page.evaluate(f"history.replaceState(null, '', '{escaped}')")
                logger.info(f"Injected params via replaceState: {final_url}")
        else:
            logger.info(f"Direct search for: '{query_text}' (tbm={tbm_val})")
            await page.get(url)

        # --- END SEARCH FLOW ---

        detected = False
        selectors = ".MjjYud, #res, .islrc, .v7W49e, .ZIN69, .g, .Gx5Zad, .WVV5ke, .PmEWq"
        for i in range(40):
            try:
                if await page.evaluate(f"document.querySelector('{selectors}') !== null"):
                    detected = True
                    break
            except Exception:
                pass

            if i < 10:
                await asyncio.sleep(0.1)
            else:
                await asyncio.sleep(0.25 + (random.random() * 0.1))

        if not detected:
            raw_check = await page.get_content()
            if is_bot_detected(page.url) or "sorry.google.com" in raw_check:
                logger.error("BOT DETECTION on result polling — rotating profile")
                await _rotate_profile()
                if len(_tried_profiles) >= len(_PROFILES):
                    logger.error("All profiles exhausted — returning 429")
                    return JSONResponse({"error": "captcha"}, status_code=429)
                logger.info(f"Retrying with next profile (tried: {_tried_profiles})")
                return await _do_search(url, _tried_profiles=_tried_profiles)

        if detected:
            try:
                is_mapped_js = f"""
                    (() => {{
                        const results = document.querySelectorAll('{selectors}');
                        if (results.length === 0) return true;

                        let mappedCount = 0;
                        let imageResults = 0;
                        const placeholder = 'R0lGODlhAQABAIA';

                        for (const res of results) {{
                            const img = res.querySelector('img');
                            if (img) {{
                                imageResults++;
                                if (img.src &&
                                    (img.src.startsWith('http') ||
                                     (img.src.startsWith('data:image') && !img.src.includes(placeholder)))) {{
                                    mappedCount++;
                                }}
                            }}
                        }}

                        if (imageResults === 0) return true;
                        return (mappedCount / imageResults) >= 0.9;
                    }})()
                """

                if tbm_val == 'vid':
                    # Video results use lazy-load — scroll to trigger full render
                    # before checking image mapping.
                    result_count_js = f"document.querySelectorAll('{selectors}').length"
                    result_count = await page.evaluate(result_count_js)

                    if result_count >= 10 and await page.evaluate(is_mapped_js):
                        logger.info("Fast-path triggered: Real images mapped")
                        await asyncio.sleep(0.4)
                    else:
                        logger.info("Slow-path: Performing smooth stepped stabilization")
                        for step in [0.25, 0.5, 0.75, 1.0]:
                            await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {step});")
                            await asyncio.sleep(0.3)

                        # Wait for result count to stabilize after scrolling
                        prev_count = 0
                        for _ in range(20):
                            current_count = await page.evaluate(result_count_js)
                            if current_count >= 10 and current_count == prev_count:
                                break
                            prev_count = current_count
                            await asyncio.sleep(0.2)

                        # Poll for image mapping to complete
                        for _ in range(30):
                            if await page.evaluate(is_mapped_js):
                                break
                            await asyncio.sleep(0.1)

                        await asyncio.sleep(0.4)
                else:
                    # Web results do not lazy-load — simple image mapping check
                    # with a short wait, no scroll required.
                    if await page.evaluate(is_mapped_js):
                        logger.info("Fast-path triggered: Real images mapped")
                    else:
                        logger.info("Waiting for image mapping to complete")
                        for _ in range(20):
                            if await page.evaluate(is_mapped_js):
                                break
                            await asyncio.sleep(0.1)
                    await asyncio.sleep(0.4)

            except Exception as e:
                logger.warning(f"Stabilization failed: {e}")
                await asyncio.sleep(1.0)

        raw_content = await page.get_content()
        content = clean_html(raw_content)

        duration = time.perf_counter() - start_perf
        logger.info(f"Done in {duration:.2f}s. Results found: {detected}")

        return HTMLResponse(content=content)

    except Exception as e:
        logger.error(f"Proxy error: {e}")
        if isinstance(e, FATAL_BROWSER_ERRORS):
            logger.warning("Fatal browser error — resetting browser process")
            await _reset_browser()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            await page.close()
            logger.info("Closed tab")
        except Exception:
            pass


@app.get('/status')
async def status():
    flagged = {i: _profile_flagged.get(i, False) for i in range(len(_PROFILES))}
    if _BROWSER_MODE == 'concurrent':
        browsers_up = {i: _browsers.get(i) is not None for i in range(len(_PROFILES))}
        return {
            'status': 'online',
            'browser_mode': 'concurrent',
            'browsers': browsers_up,
            'active_profile': _active_profile_idx,
            'profiles': flagged,
            'egress_timezone': _egress_timezone,
        }
    return {
        'status': 'online',
        'browser_mode': 'on_demand',
        'browser': _browser is not None,
        'active_profile': _active_profile_idx,
        'profiles': flagged,
        'egress_timezone': _egress_timezone,
    }
