#!/bin/sh
# Serve the interactive dashboard in docs/ on a free local port and open it.
set -eu

ROOT_DIR=$(cd "$(dirname "$0")" && pwd)
DOCS_DIR="$ROOT_DIR/docs"

# Pick a free TCP port by binding to port 0 and reading back the assignment.
PORT=$(python3 - <<'PY'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)

URL="http://127.0.0.1:$PORT/"

echo "Serving $DOCS_DIR"
echo "Dashboard at $URL"
echo "Press Ctrl-C to stop."

# Serve the docs folder, bound to localhost only.
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$DOCS_DIR" &
SERVER_PID=$!

# Stop the server on exit or interrupt.
trap 'kill "$SERVER_PID" 2>/dev/null || true' INT TERM EXIT

# Give the server a moment, then open the browser with whatever opener exists.
sleep 1
if command -v xdg-open >/dev/null 2>&1
then
  xdg-open "$URL" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1
then
  open "$URL" >/dev/null 2>&1 || true
else
  python3 -m webbrowser "$URL" >/dev/null 2>&1 || true
fi

# Wait until the server stops (Ctrl-C).
wait "$SERVER_PID"
