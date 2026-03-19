# SPDX-License-Identifier: AGPL-3.0-or-later
import random
import json
import re
import string
import time
import typing as t
from urllib.parse import unquote, urlencode
import babel
import babel.core
import babel.languages
from lxml import html
from searx import logger
from searx.enginelib.traits import EngineTraits
from searx.exceptions import SearxEngineCaptchaException
from searx.result_types import EngineResults
from searx.locales import get_official_locales, language_tag, region_tag
from searx.utils import (
    eval_xpath,
    eval_xpath_getindex,
    eval_xpath_list,
    extract_text,
    gen_gsa_useragent,
)

if t.TYPE_CHECKING:
    from searx.extended_types import SXNG_Response
    from searx.search.processors import OnlineParams

about = {
    "website": "https://www.google.com",
    "wikidata_id": "Q9366",
    "use_official_api": False,
    "require_api_key": False,
    "results": "HTML",
}

categories = ["general", "web"]
paging = True
max_page = 50
time_range_support = True
safesearch = True

time_range_dict = {"day": "d", "week": "w", "month": "m", "year": "y"}
filter_mapping = {0: "off", 1: "images", 2: "active"}
suggestion_xpath = '//div[contains(@class, "ouy7Mc")]//a'

_arcid_range = string.ascii_letters + string.digits + "_-"
_arcid_random: tuple[str, int] | None = None

def ui_async(start: int) -> str:
    global _arcid_random
    if not _arcid_random or (int(time.time()) - _arcid_random[1]) > 3600:
        _arcid_random = ("".join(random.choices(_arcid_range, k=23)), int(time.time()))
    return f"arc_id:srp_{_arcid_random[0]}_1{start:02},use_ac:true,_fmt:prog"

def get_google_info(params: "OnlineParams", eng_traits: EngineTraits) -> dict[str, t.Any]:
    ret_val = {
        "language": None, "country": None, "subdomain": None,
        "params": {}, "headers": {}, "cookies": {}, "locale": None,
    }
    sxng_locale = params.get("searxng_locale", "all")
    try:
        locale = babel.Locale.parse(sxng_locale, sep="-")
    except Exception:
        locale = None
    eng_lang = eng_traits.get_language(sxng_locale, "lang_en")
    lang_code = eng_lang.split("_")[-1]
    country = eng_traits.get_region(sxng_locale, eng_traits.all_locale)
    ret_val["language"] = eng_lang
    ret_val["country"] = country
    ret_val["locale"] = locale
    ret_val["subdomain"] = eng_traits.custom["supported_domains"].get(country.upper(), "www.google.com")
    ret_val["params"]["hl"] = f"{lang_code}-{country}"
    ret_val["params"]["lr"] = eng_lang if sxng_locale != "all" else ""
    ret_val["params"]["ie"] = "utf8"
    ret_val["params"]["oe"] = "utf8"
    ret_val["headers"]["Accept"] = "*/*"
    ret_val["headers"]["User-Agent"] = gen_gsa_useragent()
    ret_val["cookies"]["CONSENT"] = "YES+"
    return ret_val

def detect_google_sorry(resp):
    if resp.headers.get("X-Google-Captcha") == "true":
        raise SearxEngineCaptchaException(suspended_time=0)
    if resp.url.host == "sorry.google.com" or resp.url.path.startswith("/sorry"):
        raise SearxEngineCaptchaException(suspended_time=0)

def request(query: str, params: "OnlineParams") -> None:
    # 1. Build the real Google URL locally
    start = (params["pageno"] - 1) * 10
    hl = params["language"].split("-")[0]
    safe = filter_mapping.get(params["safesearch"], "images")
    google_url = f"https://www.google.com/search?q={urlencode({'q': query})[2:]}&hl={hl}&start={start}&safe={safe}"
    
    # 2. Wrap it for sxng-proxy
    proxy_url = "http://sxng-proxy:5000/search"
    params["url"] = proxy_url + "?" + urlencode({
        "url": google_url
    })
    params["cookies"] = {}
    params["headers"] = {"Accept": "text/html"}

def parse_data_images(text: str):
    data_image_map = {}
    for match in re.finditer(r"var ii=\[(.*?)\];var s='(data:image[^']*)';_setImagesSrc\(ii,s\);", text):
        ids_raw = match.group(1)
        img_data = match.group(2).replace('\\x3d', '=').replace('\\', '')
        for img_id in re.findall(r"'dimg_[^']*'", ids_raw):
            data_image_map[img_id.strip("'")] = img_data
    for match in re.finditer(r'\"(dimg_[^\"]+)\"\s*:\s*\"(data:image[^\"]+)\"', text):
        img_id = match.group(1)
        img_data = match.group(2).replace('\\u003d', '=').replace('\\', '')
        data_image_map[img_id] = img_data
    return data_image_map

def response(resp: "SXNG_Response"):
    detect_google_sorry(resp)
    data_image_map = parse_data_images(resp.text)
    results = EngineResults()
    dom = html.fromstring(resp.text)

    for result in eval_xpath_list(dom, './/div[contains(@class, "MjjYud")] | .//div[contains(@class, "Gx5Zad")] | .//div[contains(@class, "Z1YvVd")]'):
        try:
            # Title
            title_tag = eval_xpath_getindex(result, './/h3 | .//div[contains(@role, "heading")] | .//div[contains(@role, "link")]', 0, default=None)
            if title_tag is None: continue
            title = extract_text(title_tag)

            # URL
            raw_url = eval_xpath_getindex(result, ".//a/@href", 0, None)
            if raw_url is None: continue

            if raw_url.startswith('/url?q='):
                url = unquote(raw_url[7:].split("&sa=U")[0])
            else:
                url = raw_url
            
            if not url.startswith('http') and '://' not in url:
                if url.startswith('/'):
                    url = 'https://www.google.com' + url
                else: continue
            url = url.strip('\"\\')

            if '/shorts/' in url: continue

            # Content
            content_nodes = eval_xpath(result, './/div[@data-sncf="1" or @data-sncf="2" or contains(@class, "VwiC3b") or contains(@class, "fG8Fp") or contains(@class, "GAwY7c") or contains(@class, "Uo8X3b") or contains(@class, "ITZIwc")]')
            content = extract_text(content_nodes)

            thumbnail = None
            
            # YouTube Reconstruction
            yt_id_match = re.search(r'(?:v=|\/live\/|embed\/|youtu\.be\/)([0-9A-Za-z_-]{11})', url)
            if yt_id_match:
                thumbnail = f"https://img.youtube.com/vi/{yt_id_match.group(1)}/mqdefault.jpg"

            # Targeted Social/High-Res containers
            if not thumbnail:
                for img in result.xpath('.//div[contains(@class, "uhHOwf") or contains(@class, "BYbUcd")]//img'):
                    img_id = img.get('id')
                    candidate = data_image_map.get(img_id) or img.get('data-src') or img.get('src')
                    if candidate and (not candidate.startswith('data:image') or len(candidate) > 1000):
                        thumbnail = candidate
                        break

            # Global Filter Pass
            if not thumbnail:
                for img in result.xpath('.//img'):
                    if img.get('class') == 'XNo5Ab' or img.xpath('ancestor::div[contains(@class, "VuuXrf") or contains(@class, "favicon")]'):
                        continue
                    
                    img_id = img.get('id')
                    src = img.get('data-src') or img.get('src') or ''
                    candidate = data_image_map.get(img_id) or src
                    
                    if candidate:
                        if candidate.startswith('data:image'):
                            if len(candidate) > 3000: # Threshold for high-res
                                thumbnail = candidate
                                break
                        elif 'gstatic.com/images?q=tbn' not in candidate and 'favicon' not in candidate.lower():
                            thumbnail = candidate
                            break

            if thumbnail:
                thumbnail = thumbnail.replace('\\"', '').replace('\"', '').strip('\"\\')
                if 'R0lGODlhAQABAIAAAP///////yH5BAEKAAEALAAAAAABAAEAAAICTAEAOw' in thumbnail:
                    thumbnail = None

            res = {"url": url, "title": title, "content": content or '', "thumbnail": thumbnail}
            
            # Strictly Video Template (No Reels)
            is_video = False
            video_match = re.search(r'youtube\.com/(?:watch|live)|youtu\.be/|vimeo\.com/\d+|dailymotion\.com/video/', url)
            if video_match and thumbnail:
                is_video = True
            
            if is_video:
                res['template'] = 'videos.html'
                yt_id_match = re.search(r'v=([^&]+)', url)
                if yt_id_match:
                    res['iframe_src'] = f'https://www.youtube-nocookie.com/embed/{yt_id_match.group(1)}'
            
            results.append(res)
        except Exception:
            continue

    for suggestion in eval_xpath_list(dom, '//div[contains(@class, "ouy7Mc")]//a'):
        results.append({"suggestion": extract_text(suggestion)})

    return results

def fetch_traits(engine_traits: EngineTraits, add_domains: bool = True):
    from searx.network import get
    engine_traits.custom["supported_domains"] = {}
    resp = get("https://www.google.com/preferences", timeout=5)
    if not resp.ok: return
    dom = html.fromstring(resp.text.replace('<?xml version="1.0" encoding="UTF-8"?>', ""))
    for x in eval_xpath_list(dom, "//select[@name='hl']/option"):
        eng_lang = x.get("value")
        try:
            locale = babel.Locale.parse(eng_lang, sep="-")
        except Exception: continue
        sxng_lang = language_tag(locale)
        engine_traits.languages[sxng_lang] = "lang_" + eng_lang
    if add_domains:
        resp = get("https://www.google.com/supported_domains", timeout=5)
        if resp.ok:
            for domain in resp.text.split():
                domain = domain.strip()
                if not domain or domain == ".google.com": continue
                region = domain.split(".")[-1].upper()
                engine_traits.custom["supported_domains"][region] = "www" + domain
