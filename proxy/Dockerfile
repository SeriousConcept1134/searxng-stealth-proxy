FROM docker.io/debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv chromium curl xvfb gnupg2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Brave Browser (Optional but recommended)
RUN curl -fsS https://dl.brave.com/install.sh | sh

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

COPY app/ ./app/

# Warm-start entrypoint script
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
