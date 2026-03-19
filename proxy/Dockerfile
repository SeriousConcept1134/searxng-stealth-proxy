# Use a slim Debian base
FROM docker.io/debian:bookworm-slim

# Combine updates, essential tools, and Brave into one layer for maximum size reduction
# Chromium is removed to save ~500MB
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv curl xvfb gnupg2 ca-certificates && \
    curl -fsS https://dl.brave.com/install.sh | sh && \
    apt-get purge -y curl gnupg2 && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Use --no-cache-dir to keep the image lean
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY app/ ./app/

# Entrypoint script: cleans up locks and starts the proxy
RUN printf "#!/bin/bash\n\
rm -f /tmp/.X99-lock\n\
rm -f /data/brave_profile/Singleton*\n\
mkdir -p /tmp/brave_work_profile\n\
cp -a /data/brave_profile/. /tmp/brave_work_profile/\n\
Xvfb :99 -screen 0 1280x1024x24 &\n\
export DISPLAY=:99\n\
export BRAVE_PROFILE=/tmp/brave_work_profile\n\
uvicorn app.proxy_service:app --host 0.0.0.0 --port 5000" > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
