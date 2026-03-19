# SearXNG Stealth Proxy (Standalone Sidecar)

This project provides a standalone browser-based stealth proxy to restore **Google** and **Google Videos** functionality in any existing SearXNG instance.

## 🚀 Features
- **Bypass 403/429 Blocks**: Uses `nodriver` (Brave/Chrome) to simulate organic user behavior.
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
```bash
cp .env.example .env
```

Find your existing SearXNG network name so the proxy can "plug in" to it:
- **Docker**: `docker network ls`
- **Podman**: `podman network ls`

> **Note**: Compose usually prefixes network names with your folder name (e.g. `searxng_searxng-net`). Use the **full name** as it appears in the `NAME` column of the command output.

Edit the `.env` file and set `EXTERNAL_NETWORK` to that full name.

### 2. Start the Proxy Container
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

### 3. Profile Warming (CAPTCHA Solving)
This step is **MANDATORY** to prevent Google from blocking the container immediately.

#### **Linux Setup:**
```bash
./scripts/setup.sh
./venv/bin/python scripts/manage.py
```

#### **Windows Setup:**
```powershell
.\scripts\setup.ps1
.\venv\Scripts\python.exe scripts\manage.py
```

- The script will intelligently scan your system for installed Chromium browsers (Brave, Chrome, Edge, Vivaldi, etc.).
- If multiple browsers are found, you will be prompted to choose which one to use.
- A browser window will open on your host machine.
- **Note**: If you are using Warp, the browser will automatically use the Warp IP via the `HOST_PROXY_URL` you configured.
- Solve any CAPTCHAs, then **close the browser window**.
- The script will automatically clear locks and restart the proxy container for you.

### 4. Integrate with SearXNG
Modify your SearXNG `docker-compose.yaml` to mount the patches over the core files:

```yaml
services:
  searxng:
    # ... existing config ...
    volumes:
      # Mount the patches
      - ./patches/google.py:/usr/local/searxng/searx/engines/google.py:ro
      - ./patches/google_videos.py:/usr/local/searxng/searx/engines/google_videos.py:ro
      - ./patches/client.py:/usr/local/searxng/searx/network/client.py:ro
```

#### 🛠️ Force Recompilation (Required)
SearXNG images often ship with pre-compiled bytecode that will ignore your mounted `.py` files. **You must run this command** after your first install and after every image update to force SearXNG to use the patches:

```bash
# For Docker (replace 'docker' with 'podman' if needed)
docker exec searxng sh -c "
  rm -rf /usr/local/searxng/searx/engines/__pycache__ \
         /usr/local/searxng/searx/network/__pycache__ && \
  /usr/local/searxng/.venv/bin/python3 -m compileall \
         /usr/local/searxng/searx/engines/ \
         /usr/local/searxng/searx/network/
" && docker restart searxng
```

#### Significance of the Patches:
- **`google.py`**: Redirects all standard Google searches to the `sxng-proxy` container and updates the XPaths to match the modern desktop layout returned by the browser.
- **`google_videos.py`**: Implements the **Nest Hub UA** strategy. This triggers a specific legacy layout in Google that allows for the extraction of rich metadata (Views, Author, Duration) without complex JS execution. It also constructs high-resolution YouTube thumbnails.
- **`client.py`**: Critically modifies SearXNG's network layer. It whitelists the local proxy container to allow persistent HTTP connections. Without this, SearXNG's security layer would drop the connection to the proxy, leading to 500 errors.

Update your `settings.yml` with the proxy details from the `patches/settings.yml.example` provided.

## 🧠 Technical Architecture

This proxy is designed to be a "Stealth Sidecar." Here is how it works under the hood:

### 1. X11 Virtual Display (Xvfb)
Even when running in "headless" mode, modern browsers and bot-detection scripts often behave differently if no display is detected. 
- The container runs **Xvfb** (X Virtual Framebuffer) to create a virtual screen (Display `:99`).
- This ensures that Chromium/Brave has a valid rendering target, which helps in bypassing certain "headless-detection" fingerprints and ensures stability for automation.

### 2. Stealth via `nodriver`
The proxy uses the `nodriver` library, which communicates directly with the browser via the Chrome DevTools Protocol (CDP). 
- It avoids using Selenium's `webdriver` flags, which are easily detected by Google.
- It manages execution timing and event handling to simulate human-like interaction.

### 3. "Warm Start" Profile Mirroring
To ensure high availability and prevent data corruption:
- Your "Master Profile" (containing the CAPTCHA session) is stored at `/data/brave_profile`.
- On startup, the container **mirrors** this profile to a temporary working directory in `/tmp`.
- This prevents the dreaded `SingletonLock` errors (which happen if a browser process crashes) and ensures that the container remains stateless and disposable.

### 4. Surgical Patching vs. Image Rebuilding
Instead of forcing users to maintain a custom SearXNG image, we use **Volume Overlays**. 
- The `.py` files in `/patches` are mounted directly over the ones inside the standard SearXNG container.
- This allows you to update SearXNG normally while keeping the proxy integration intact.

## 🔄 Maintenance
If you start seeing "403" or "Captcha" results in SearXNG, simply run:
```bash
# Linux
./venv/bin/python scripts/manage.py
# Windows
.\venv\Scripts\python.exe scripts\manage.py
```
Solve the challenge, close the browser, and you are back online!
