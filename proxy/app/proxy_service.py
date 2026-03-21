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

# Serialise requests to avoid concurrent Google hits from the same session
_search_semaphore = asyncio.Semaphore(1)
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 3.5
_MAX_REQUEST_JITTER = 2.5

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
_browser: uc.Browser | None = None


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
    """Return the active browser, starting a new one if needed."""
    global _browser, _active_profile_idx, _egress_timezone

    if not _browser:
        if not _PROFILES:
            _init_profile_pool()

        profile, idx = _get_next_healthy_profile()
        _active_profile_idx = idx
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

        _browser = await uc.start(
            user_data_dir=profile,
            browser_executable_path='/usr/bin/brave-browser-stable',
            headless=True,
            browser_args=args
        )

        _egress_timezone = await detect_egress_timezone(proxy)

    return _browser


async def _reset_browser() -> None:
    """Stop the current browser and clear the global reference."""
    global _browser
    if _browser:
        try:
            await _browser.stop()
        except Exception:
            pass
    _browser = None


async def _rotate_profile() -> None:
    """Flag the active profile, write the warmup marker, and reset the browser."""
    _flag_active_profile()
    await _reset_browser()


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
    _init_profile_pool()
    idle = int(os.environ.get('STARTUP_IDLE_SECONDS', '0'))
    if idle > 0:
        logger.info(f"Startup idle: waiting {idle}s before accepting requests")
        await asyncio.sleep(idle)


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


async def _do_search(url: str) -> HTMLResponse | JSONResponse:
    start_perf = time.perf_counter()
    b = await get_browser()
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
        })))
        await page.send(cdp_page.add_script_to_evaluate_on_new_document(
            source=_NAVIGATOR_OVERRIDES
        ))
        await page.send(emulation.set_timezone_override(
            timezone_id=_egress_timezone
        ))

        # --- HUMANIZED SEARCH FLOW ---
        parsed_incoming = urlparse(url)
        params = parse_qs(parsed_incoming.query)

        query_text = params.get('q', [''])[0]
        start_val = params.get('start', ['0'])[0]
        safe_val = params.get('safe', [''])[0]
        tbm_val = params.get('tbm', [''])[0]
        hl_val = params.get('hl', ['en'])[0]

        if not query_text:
            logger.warning(f"No query found in URL: {url}")
            return JSONResponse({"error": "no_query"}, status_code=503)

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
            logger.error(f"BOT DETECTION on submission — rotating profile")
            await _rotate_profile()
            return JSONResponse({"error": "captcha"}, status_code=429)

        validated_url = page.url
        logger.info(f"Obtained validated URL: {validated_url}")

        final_url = inject_params(validated_url, start_val, safe_val)
        if final_url != validated_url:
            escaped = final_url.replace("'", "\\'")
            await page.evaluate(f"history.replaceState(null, '', '{escaped}')")
            logger.info(f"Injected params via replaceState: {final_url}")

        # --- END HUMANIZED SEARCH FLOW ---

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
                return JSONResponse({"error": "captcha"}, status_code=429)

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

                if await page.evaluate(is_mapped_js):
                    logger.info("Fast-path triggered: Real images mapped")
                    await asyncio.sleep(0.4)
                else:
                    logger.info("Slow-path: Performing smooth stepped stabilization")
                    for step in [0.25, 0.5, 0.75, 1.0]:
                        await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {step});")
                        await asyncio.sleep(0.3)

                    for _ in range(30):
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
    return {
        'status': 'online',
        'browser': _browser is not None,
        'active_profile': _active_profile_idx,
        'profiles': flagged,
    }
