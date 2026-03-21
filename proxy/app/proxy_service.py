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
browser = None

UA_FILE = '/app/patches/gsa_useragents.txt'
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.7680.153 Safari/537.36"
)
FATAL_BROWSER_ERRORS = (ConnectionRefusedError, ProcessLookupError, BrokenPipeError)


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


async def submit_search(page, search_input) -> bool:
    """Submit the search form and wait for navigation to results.

    Tries Enter key first, falls back to JS form submit if navigation
    does not occur within the expected window.
    """
    await search_input.click()
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


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.error(f"VALIDATION ERROR: {exc.errors()}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


async def get_browser():
    global browser
    if not browser:
        profile = os.environ.get('BRAVE_PROFILE', '/data/brave_profile')
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

        logger.info(f"Initializing Stable Stealth Browser with profile: {profile}")

        browser = await uc.start(
            user_data_dir=profile,
            browser_executable_path='/usr/bin/brave-browser-stable',
            headless=True,
            browser_args=args
        )
    return browser


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


@app.get('/search')
async def search(request: Request):
    url = request.query_params.get('url')

    if not url:
        raise HTTPException(status_code=400, detail="Missing url parameter")

    start_perf = time.perf_counter()
    b = await get_browser()
    page = await b.get(new_tab=True)

    try:
        target_ua = load_ua()

        import nodriver.cdp.network as network
        await page.send(network.set_user_agent_override(user_agent=target_ua))

        # --- HUMANIZED SEARCH FLOW ---
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

        parsed_incoming = urlparse(url)
        params = parse_qs(parsed_incoming.query)

        query_text = params.get('q', [''])[0]
        start_val = params.get('start', ['0'])[0]
        safe_val = params.get('safe', [''])[0]
        tbm_val = params.get('tbm', [''])[0]
        hl_val = params.get('hl', ['en'])[0]

        if not query_text:
            logger.warning(f"No query found in URL: {url}")
            await page.get(url)
        else:
            entry_url = f"https://www.google.com/webhp?hl={hl_val}"
            if tbm_val == 'vid':
                entry_url += "&tbm=vid"

            logger.info(f"Humanizing search for: '{query_text}' (tbm={tbm_val})")
            await page.get(entry_url)

            search_input = await page.select('textarea[name="q"], input[name="q"]', timeout=5)
            if not search_input:
                logger.error("Could not find search input field!")
                await page.get(url)
            else:
                await search_input.send_keys(query_text)
                await asyncio.sleep(0.2)

                navigated = await submit_search(page, search_input)

                if not navigated:
                    logger.error("Search submission failed to trigger navigation, falling back to direct URL")
                    await page.get(url)
                else:
                    # Early bot detection: catch /sorry/ redirects before proceeding
                    if is_bot_detected(page.url):
                        logger.error(f"BOT DETECTION on submission — sorry page: {page.url}")
                        return JSONResponse({"error": "captcha"}, status_code=429)

                    validated_url = page.url
                    logger.info(f"Obtained validated URL: {validated_url}")

                    v_parsed = urlparse(validated_url)
                    v_params = parse_qs(v_parsed.query)

                    needs_reload = False
                    if start_val != '0' and v_params.get('start', [''])[0] != start_val:
                        v_params['start'] = [start_val]
                        needs_reload = True
                    if safe_val and v_params.get('safe', [''])[0] != safe_val:
                        v_params['safe'] = [safe_val]
                        needs_reload = True

                    if needs_reload:
                        final_query = urlencode(v_params, doseq=True)
                        final_url = urlunparse(v_parsed._replace(query=final_query))
                        logger.info(f"Re-injecting parameters: {final_url}")
                        await page.get(final_url)
                    else:
                        logger.info("Validated URL is already correct.")

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

        # Late bot detection: catch sorry pages that slipped past the URL check
        if not detected:
            raw_check = await page.get_content()
            if is_bot_detected(page.url) or "sorry.google.com" in raw_check:
                logger.error("BOT DETECTION on result polling — returning 429")
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
            global browser
            logger.warning("Fatal browser error — resetting browser process")
            if browser:
                try:
                    await browser.stop()
                except Exception:
                    pass
            browser = None
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            await page.close()
            logger.info("Closed tab")
        except Exception:
            pass


@app.get('/status')
async def status():
    return {'status': 'online', 'browser': browser is not None}
