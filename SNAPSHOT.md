# SearXNG Stealth Proxy: Project Snapshot (March 19, 2026)

## Current Status
- **Goal**: Restore Google and Google Videos in SearXNG via a sidecar container using high-fidelity extraction.
- **State**: Stabilized. Both engines are functional with correct pagination and thumbnail logic.

## "Golden" Logic (DO NOT ALTER WITHOUT APPROVAL)
- **Proxy Communication**: Engine sends a full Google URL to the proxy via the `url` query parameter.
- **Response Format**: Proxy returns **Raw HTML** (using `HTMLResponse` in FastAPI) after a robust wait loop (2 seconds post-detection of results).
- **Cleanup**: Proxy uses `clean_html` to strip styles/iframes but **strictly preserves** scripts containing `_setImagesSrc`, `dimg_`, and `google.ldi/pim` data.
- **Thumbnail Filtering**:
  - **YouTube**: Primary reconstruction via ID (`mqdefault.jpg`).
  - **Social/General**: Targeted pass for `uhHOwf` and `BYbUcd` containers.
  - **Favicon Killer**: Strictly skip images with class `XNo5Ab` or inside `VuuXrf`. Enforce a **base64 length threshold of > 3000 chars** for any non-video result thumbnail.
- **Pagination**: Calculated as `start = (pageno - 1) * 10` and passed as a string in the built URL.
- **Social Layouts**: Instagram Reels are treated as **regular results** (not `videos.html`) to ensure correct rendering and high-res thumbnail mapping.

## Infrastructure
- **Proxy**: `sxng-proxy` container running FastAPI/nodriver.
- **SearXNG**: Connected via `searxng-docker_searxng-net`.
- **Patches**: Volume-mounted to `/usr/local/searxng/searx/engines/google.py` and `google_videos.py`.

## Next Steps
- Address minor UI issues.
- Monitor for any new Google layout shifts.
