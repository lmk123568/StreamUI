#!/bin/bash

echo "🚀 Starting StreamUI ..."

# start nginx
nginx -p /workspace/frontend -c /workspace/frontend/nginx.conf -g 'daemon off;' &

# start fastapi
cd /workspace/backend
python main.py &

echo "✅ All services started. Awaiting termination..."
wait -n
