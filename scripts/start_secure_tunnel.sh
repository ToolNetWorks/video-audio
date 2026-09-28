#!/bin/bash
# Start cloudflared tunnel and write the URL to a file

LOG_FILE="/tmp/cloudflared.log"
URL_FILE="/var/lib/loop-video-audio/public_url.txt"

# Kill existing
pgrep -af cloudflared | grep -v start_secure_tunnel | awk '{print $1}' | xargs -r kill -9

echo "Starting cloudflared..."
cloudflared tunnel --url http://localhost:9090 > $LOG_FILE 2>&1 &
TUNNEL_PID=$!

echo "Waiting for tunnel URL..."
for i in {1..20}; do
    URL=$(grep "trycloudflare.com" $LOG_FILE | grep -o 'https://[-a-zA-Z0-9]*\.trycloudflare\.com' | head -1)
    if [ ! -z "$URL" ]; then
        echo "Found URL: $URL"
        echo "$URL" > $URL_FILE
        chmod 644 $URL_FILE
        exit 0
    fi
    sleep 1
done

echo "Failed to get tunnel URL."
exit 1
