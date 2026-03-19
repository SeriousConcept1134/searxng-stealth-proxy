# Investigation: Intermittent Zero-Results & Performance Lag

## 1. Observed Symptoms
- **Successful Search**: Results returned in **2.7s – 3.0s**.
- **Failed Search**: Search takes **~11.5s**, returning **0 results** in the frontend with no explicit error message (no HTTP error, no access denied).
- **Engine Bias**: Google Videos (`!gov`) is significantly more consistent than Google Web (`!go`).

## 2. Root Cause Analysis
The investigation of the `sxng-proxy` and `searxng` container logs revealed the following:

### A. The 11-Second Delay
The proxy service is configured with a **10-second timeout** to wait for result containers (like `<h3>` tags) to appear. 
- When Google serves a valid search page, the containers appear in < 3s.
- When Google flags the request as "unusual traffic," it serves a **CAPTCHA/interstitial page**.
- This page **does not contain** the expected result selectors.
- The proxy waits for the full 10 seconds, fails to find the markers, and then proceeds to return the current page content.

### B. The Zero-Result Frontend
- The proxy returns the HTML of the Google CAPTCHA page with a **`200 OK`** status.
- SearXNG receives this HTML and attempts to parse it using the `google.py` logic.
- Since the CAPTCHA page has no `MjjYud` or `Gx5Zad` containers, the extraction loop finds nothing.
- SearXNG treats this as a "successful search with no matches," hence no error is displayed.

### C. Pattern of Recovery
The system recovers without a restart because the Brave profile eventually enters a "trusted" state again, or Google's temporary rate-limit/IP-flag expires.

## 3. Comparison of Requests

| Metric | Successful Request | Failed Request |
| :--- | :--- | :--- |
| **Duration** | 2.7s - 3.5s | 10.8s - 12.1s |
| **Proxy Log** | `Results found: True` | `Results found: False` |
| **Payload** | Standard Search Results | Google "Sorry" / CAPTCHA Page |
| **Frontend** | 10+ Results | 0 Results |

## 4. Conclusions
The system logic is sound and functional. The performance degradation and missing results are entirely due to **intermittent blocking/interstitials from Google**. 

The reason `!gov` is more consistent is likely due to Google's bot-detection heuristics being different for the video-specific layout/subdomain than for general web search.
