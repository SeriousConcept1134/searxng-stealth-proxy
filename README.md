# SearXNG Stealth Proxy (Standalone Sidecar)

This project provides a standalone browser-based stealth proxy to restore **Google** and **Google Videos** functionality in any existing SearXNG instance.

## 🚀 Features

- **Bypass 403/429 Blocks**: Uses `nodriver` (Brave/Chrome) with full CDP fingerprint hardening to avoid bot detection.
- **Multi-Profile Rotation**: Maintains a pool of 3 warmed browser profiles with automatic failover on CAPTCHA detection.
- **Configurable Search Mode**: Switch between fast direct URL navigation and a full humanized search simulation.
- **High-Fidelity Metadata**: Restores views, dates, and author information for Google Videos.
- **IP Rotation (Optional)**: Includes a Cloudflare WARP profile for IP cleanliness.
- **Surgical Patching**: Easy integration via Docker Volume Overlays.

## 📋 Prerequisites

- Docker or Podman
- Python 3.x
- **Chromium-based Browser** installed on the host (Brave, Google Chrome, Microsoft Edge, Vivaldi, etc.) for manual CAPTCHA solving.

## 🛠️ Setup Instructions

### 1. Configure the Proxy

Clone this repo and copy the example environment file:

**Linux / macOS:**
```bash
cp .env.example .env
```

**Windows (PowerShell):**
```powershell
Copy-Item .env.example .env
```

Find your existing SearXNG network name so the proxy can "plug in" to it:

- **Docker**: `docker network ls`
- **Podman**: `podman network ls`

> **Note**: Compose usually prefixes network names with your folder name (e.g. `searxng-docker_searxng-net`). Use the **full name** as it appears in the `NAME` column of the command output.

Edit the `.env` file and set `EXTERNAL_NETWORK` to that full name.

### 2. Create Profile Directories

The proxy uses a pool of 3 browser profiles. Create the directories before starting:

**Linux / macOS:**
```bash
mkdir -p data/brave_profile_0 data/brave_profile_1 data/brave_profile_2
```

**Windows (PowerShell):**
```powershell
New-Item -ItemType Directory -Force -Path data\brave_profile_0, data\brave_profile_1, data\brave_profile_2
```

### 3. Start the Proxy Container

Choose the path that matches your current setup (use `podman-compose` if on Podman):

#### Path A: No Proxy (Direct Connection)

Use this if you don't want to use a VPN/Warp and will use your home/server IP.

- Edit `.env`: Set `PROXY_URL=` (leave empty).
- Run: `docker-compose --profile standard up -d`

#### Path B: New Warp Setup (Starting from scratch)

Use this if you want to set up a fresh Warp proxy along with the stealth proxy.

- Edit `.env`:
  - `PROXY_URL=socks5://searxng-warp:1080`
  - `HOST_PROXY_URL=socks5://127.0.0.1:1080`
- Run: `docker-compose --profile warp up -d`

#### Path C: Existing Warp/Proxy (Integration)

Use this if you already have a Warp container (like `docker-warp-socks`) running in your SearXNG stack.

- **Requirement**: Your existing Warp container **must** expose its SOCKS5 port to your host (e.g., `-p 127.0.0.1:1080:9091`).
- Edit `.env`:
  - `EXTERNAL_NETWORK`: Set to your existing SearXNG network name.
  - `PROXY_URL`: Set to your existing Warp **service name** and **internal port** (e.g., `socks5://warp-proxy:9091`).
  - `HOST_PROXY_URL`: Set to your local host IP/port (e.g., `socks5://127.0.0.1:1080`).
- Run: `docker-compose --profile standard up -d`

### 4. Profile Warming (CAPTCHA Solving)

This step is **MANDATORY** before the proxy can serve requests. Each profile in the pool must be warmed independently.

#### **Linux Setup:**

```bash
./scripts/setup.sh
./venv/bin/python scripts/manage.py
```

#### **Windows Setup:**

> **Note**: Before running the setup script for the first time, you may need to allow PowerShell to execute local scripts. Run this once in an elevated PowerShell window (Run as Administrator):
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

Run the setup script from the **repo root**:

```powershell
.\scripts\setup.ps1
```

Then run the warmup script, also from the **repo root**:

```powershell
.\venv\Scripts\python.exe scripts\manage.py
```

The warmup script will:

- Auto-detect installed Chromium browsers on your system (prompting if multiple are found).
- Determine which profiles need warmup by checking for `.needs_warmup` marker files.
- If profiles are flagged (CAPTCHA-triggered), proceed directly to the CAPTCHA prompt for each.
- If no profiles are flagged, prompt you to select which profile to warm up.
- For fresh profile setups, automatically run **6 seed queries** across Google Search, Google News, Google Maps, and Google Video to build session history before handing off to you.
- Open a browser window on your host — solve any CAPTCHAs presented, then close the window.
- Automatically clear locks and restart the proxy container.

**Warm all 3 profiles** before relying on the rotation pool. Re-run the script for each profile index (0, 1, 2).

### 5. Integrate with SearXNG

Modify your SearXNG `docker-compose.yaml` to mount the patches over the core files and add the entrypoint override that automatically clears stale bytecode on every startup:

```yaml
services:
  searxng:
    # ... existing config ...
    entrypoint:
      - sh
      - -c
      - |
        rm -f /usr/local/searxng/searx/engines/__pycache__/*.pyc
        rm -f /usr/local/searxng/searx/network/__pycache__/*.pyc
        exec /usr/local/searxng/entrypoint.sh
    volumes:
      - ./patches/google.py:/usr/local/searxng/searx/engines/google.py:ro
      - ./patches/google_videos.py:/usr/local/searxng/searx/engines/google_videos.py:ro
      - ./patches/client.py:/usr/local/searxng/searx/network/client.py:ro
      - ./patches/gsa_useragents.txt:/usr/local/searxng/searx/data/gsa_useragents.txt:ro
```

The entrypoint override clears any pre-compiled `.pyc` files from the image before SearXNG starts, ensuring your mounted `.py` patches are always used. This runs automatically on every container startup — no manual recompilation needed, even after image updates or system restarts.

#### Significance of the Patches:

- **`google.py`**: Redirects all standard Google searches to the `sxng-proxy` container. Handles 429 (CAPTCHA) and 503 (flow failure) responses from the proxy with appropriate SearXNG exceptions.
- **`google_videos.py`**: Same proxy integration as `google.py`, applied to the video search engine.
- **`client.py`**: Modifies SearXNG's network layer to whitelist the local proxy container for persistent HTTP connections.
- **`gsa_useragents.txt`**: Contains the User-Agent string used by the proxy browser — a native Brave/Chromium UA matching the actual engine.

Update your `settings.yml` with the proxy details from the `patches/settings.yml.example` provided.

## ⚙️ Configuration

All configuration is via `.env`. See `.env.example` for the full reference. Key variables:

| Variable | Default | Description |
|---|---|---|
| `EXTERNAL_NETWORK` | `searxng-net` | Docker/Podman network shared with SearXNG |
| `PROXY_URL` | _(empty)_ | SOCKS5 proxy URL for the container (e.g. WARP) |
| `HOST_PROXY_URL` | _(empty)_ | SOCKS5 proxy URL accessible from the host (for warmup) |
| `SEARCH_MODE` | `direct` | `direct` for fast URL navigation, `humanized` for homepage simulation |
| `STARTUP_IDLE_SECONDS` | `0` | Seconds to wait after startup before accepting requests |
| `BROWSER_PATH` | _(auto-detect)_ | Override the browser binary used by the warmup script |

### Search Mode

The proxy supports two search modes, switchable via `SEARCH_MODE` in `.env`:

- **`direct`** (default) — navigates straight to the Google search URL. Fast (~2–3s), recommended for normal use. All CDP fingerprint hardening still applies.
- **`humanized`** — simulates organic search by navigating to the Google homepage, typing the query, and submitting the form. Slower (~8–12s) but mimics human browsing more closely. Use if direct mode triggers frequent CAPTCHAs.

### Post-Warmup Idle Delay

After a CAPTCHA-triggered re-warm, set `STARTUP_IDLE_SECONDS=600` in `.env` before restarting the container. This gives the freshly activated cookie session 10 minutes to age before automated queries hit it. Reset to `0` for normal restarts.

## 🧠 Technical Architecture

### 1. X11 Virtual Display (Xvfb)

The container runs **Xvfb** (X Virtual Framebuffer) at `1920x1080` to provide a valid rendering target for Brave, avoiding headless-detection signals that fire when no display is present.

### 2. Fingerprint Hardening via CDP

The proxy applies a comprehensive set of CDP overrides per browser tab to present a consistent, non-automated fingerprint:

- **Native Brave/Chromium UA** matching the actual browser engine version (no UA/engine mismatch)
- **`Sec-CH-UA` client hint headers** normalized to match the UA string
- **`navigator.webdriver`** removed; `plugins` and `languages` normalized
- **Timezone** auto-detected from the WARP egress IP via `ip-api.com` at browser startup
- **Viewport** enforced via `Emulation.setDeviceMetricsOverride` at `1920x1080`
- **Request serialization** via `asyncio.Semaphore` with 3.5–6s randomized inter-request jitter

### 3. Multi-Profile Rotation Pool

The proxy maintains a pool of 3 browser profiles (`brave_profile_0`, `brave_profile_1`, `brave_profile_2`):

- On CAPTCHA detection, the active profile is **flagged** and a `.needs_warmup` marker file is written into its directory.
- The proxy automatically **rotates** to the next healthy profile without downtime.
- Flagged profiles run periodic automated recovery checks — if Google no longer presents a CAPTCHA, the flag is cleared automatically without manual intervention.
- The `/status` endpoint exposes pool state including which profiles are flagged.
- The warmup script reads marker files to determine which profiles need attention.

### 4. Profile Persistence

Profile data is volume-mounted directly at `/data/brave_profile_0/1/2` and used in-place — no tmp copy is made. Session data (cookies, trust history) **accumulates and persists across container restarts**, which is important for maintaining Google's session trust score.

### 5. Session Keepalive

The proxy runs a background keepalive loop for each profile, visiting `google.com/webhp` on a randomized 18–28 minute interval to refresh session activity signals. This prevents inactivity-triggered CAPTCHA challenges that occur when a warm profile sits idle for extended periods.

### 6. Surgical Patching vs. Image Rebuilding

The `.py` files in `/patches` are mounted directly over the standard SearXNG container files via Docker volume overlays. This allows you to update SearXNG normally while keeping the proxy integration intact.

## 🔄 Maintenance

If you see "403", "Captcha", or 0-result responses in SearXNG, the proxy has detected a bot challenge and rotated to the next profile. Check the proxy logs:

**Linux / macOS:**
```bash
podman logs sxng-proxy --tail 50
# or
docker logs sxng-proxy --tail 50
```

**Windows:**
```powershell
docker logs sxng-proxy --tail 50
```

Look for `Profile X flagged — re-warm required`. Flagged profiles will attempt automated recovery on each keepalive cycle. If you prefer to re-warm manually, run the warmup script:

**Linux / macOS:**
```bash
./venv/bin/python scripts/manage.py
```

**Windows:**
```powershell
.\venv\Scripts\python.exe scripts\manage.py
```

The script will queue all flagged profiles, warm each one in sequence (skipping seed queries since the session is already blocked), and restart the proxy when done.

## 📊 Status Endpoint

The proxy exposes a `/status` endpoint at `http://localhost:5000/status`:

```json
{
  "status": "online",
  "browser": true,
  "active_profile": 0,
  "profiles": {
    "0": false,
    "1": false,
    "2": true
  }
}
```

`true` in the profiles map indicates that profile is flagged and needs re-warming.
