from fastapi import FastAPI, HTTPException, Query, Request
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

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.error(f"VALIDATION ERROR: {exc.errors()}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

async def get_browser():
    global browser
    if not browser:
        profile = os.environ.get('BRAVE_PROFILE', '/data/brave_profile')
        proxy = os.environ.get('PROXY_URL', '')
        
        # Human-like arguments to bypass bot detection
        args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled", # Hides the 'navigator.webdriver' flag
            "--disable-infobars",
            "--window-size=1920,1080",
            "--start-maximized",
            "--disable-features=IsolateOrigins,site-per-process", # Helps with cross-domain framing
            "--disable-gpu" if os.name != 'nt' else "--enable-gpu", # Containers usually lack GPU
        ]
        
        if proxy:
            args.append(f'--proxy-server={proxy}')
        
        logger.info(f"Initializing Stealth Browser with profile: {profile}")
        
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
            text = script.text or ""
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
    ua = request.query_params.get('ua', '')
    
    if not url:
        raise HTTPException(status_code=400, detail="Missing url parameter")
        
    start_perf = time.perf_counter()
    b = await get_browser()
    page = b.main_tab
    
    try:
        # If SearXNG provided a UA, use it, otherwise use a high-quality default
        target_ua = ua or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        
        import nodriver.cdp.network as network
        await page.send(network.set_user_agent_override(user_agent=target_ua))
        
        logger.info(f"Visiting: {url}")
        await page.get(url)
        
        # 1. Wait for result containers with jitter
        detected = False
        selectors = ".MjjYud, #res, .islrc, .v7W49e, .ZIN69, .g, .Gx5Zad, .WVV5ke"
        for _ in range(40):
            try:
                if await page.evaluate(f"document.querySelector('{selectors}') !== null"):
                    detected = True
                    break
            except: pass
            await asyncio.sleep(0.25 + (random.random() * 0.1)) # Add jitter
            
        # 2. Trigger lazy-loading by scrolling
        if detected:
            try:
                # Scroll to bottom then back up to ensure all lazy elements trigger
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight/2);")
                await asyncio.sleep(0.5)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                await asyncio.sleep(1.5 + random.random()) 
            except:
                await asyncio.sleep(2.0)
            
        raw_content = await page.get_content()
        
        # Safety Check: If we see "sorry.google.com" in the content, we were caught
        is_captcha = "sorry.google.com" in raw_content or "captcha" in raw_content.lower()
        if is_captcha:
            logger.error("BOT DETECTION TRIGGERED")
        
        content = clean_html(raw_content)
        
        duration = time.perf_counter() - start_perf
        logger.info(f"Done in {duration:.2f}s. Results found: {detected}")
        
        headers = {"X-Google-Captcha": "true"} if is_captcha else {}
        return HTMLResponse(content=content, headers=headers)
        
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        # Only crash the browser on actual connection/internal errors
        global browser
        if browser:
            try: await browser.stop()
            except: pass
        browser = None
        raise HTTPException(status_code=500, detail=str(e))

@app.get('/status')
async def status():
    return {'status': 'online', 'browser': browser is not None}
