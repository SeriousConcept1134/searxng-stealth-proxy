from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
import nodriver as uc
import asyncio
import os
import time
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
    logger.error(f"RAW QUERY PARAMS: {request.query_params}")
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body": str(exc.body)},
    )

async def get_browser():
    global browser
    if not browser:
        profile = os.environ.get('BRAVE_PROFILE', '/data/brave_profile')
        proxy = os.environ.get('PROXY_URL', '')
        args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-gpu",
            "--window-size=1920,1080",
        ]
        if proxy:
            args.append(f'--proxy-server={proxy}')
        
        logger.info(f"Initializing browser: /usr/bin/brave-browser-stable with profile: {profile}")
        
        # Explicitly use the stable binary path with the correct parameter name
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
        
        # Remove heavy non-essential tags
        tags_to_strip = ['style', 'svg', 'noscript', 'header', 'footer', 'iframe', 'canvas']
        for tag_name in tags_to_strip:
            for tag in dom.xpath(f'//{tag_name}'):
                tag.getparent().remove(tag)
        
        # Selective script removal: Keep scripts containing thumbnail data
        for script in dom.xpath('//script'):
            text = script.text or ""
            if "google.ldi" in text or "google.pim" in text or "dimg_" in text or "_setImagesSrc" in text:
                continue # Keep these for thumbnails
            script.getparent().remove(script)
            
        return html.tostring(dom, encoding='unicode')
    except Exception as e:
        logger.warning(f"Cleanup failed: {e}")
        return content

@app.get('/search')
async def search(request: Request):
    # Manually extract parameters to bypass validation issues
    url = request.query_params.get('url')
    ua = request.query_params.get('ua', '')
    
    if not url:
        logger.error(f"Missing url in params: {request.query_params}")
        raise HTTPException(status_code=400, detail="Missing url parameter")
        
    start_perf = time.perf_counter()
    b = await get_browser()
    page = b.main_tab
    
    try:
        # Override User-Agent if requested
        if ua:
            import nodriver.cdp.network as network
            await page.send(network.set_user_agent_override(user_agent=ua))
        
        logger.info(f"Visiting: {url}")
        await page.get(url)
        
        # 1. Wait for result containers
        detected = False
        selectors = ".MjjYud, #res, .islrc, .v7W49e, .ZIN69, .g, .Gx5Zad"
        for _ in range(50):
            try:
                if await page.evaluate(f"document.querySelector('{selectors}') !== null"):
                    detected = True
                    break
            except:
                pass
            await asyncio.sleep(0.2)
            
        # 2. Wait a bit more for lazy-loaded content
        if detected:
            await asyncio.sleep(2.0)
            
        raw_content = await page.get_content()
        content = clean_html(raw_content)
        
        # Reset User-Agent to default after request
        if ua:
            await page.send(network.set_user_agent_override(user_agent=""))
            
        duration = time.perf_counter() - start_perf
        logger.info(f"Done in {duration:.2f}s. Results found: {detected}")
        
        return HTMLResponse(content=content)
        
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        global browser
        if browser:
            try: await browser.stop()
            except: pass
        browser = None
        raise HTTPException(status_code=500, detail=str(e))

@app.get('/status')
async def status():
    return {'status': 'online', 'browser': browser is not None}
