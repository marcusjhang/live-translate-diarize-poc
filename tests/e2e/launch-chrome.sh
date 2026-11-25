#!/usr/bin/env bash
# Start a headless Chrome for the e2e suites and write its page CDP URL to $CDP_FILE
# (default /tmp/cdp.txt). Alternative to the Superset pane in README.md.
#   tests/e2e/launch-chrome.sh            # start (idempotent)
#   tests/e2e/launch-chrome.sh stop
set -eu
PORT=${CDP_PORT:-9333}
CDP_FILE=${CDP_FILE:-/tmp/cdp.txt}
PROFILE=${CHROME_PROFILE:-/tmp/e2e-chrome-profile}
CHROME=${CHROME:-"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}
PIDFILE=/tmp/e2e-chrome.pid

if [ "${1:-}" = "stop" ]; then
  [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null || true
  rm -f "$PIDFILE"; exit 0
fi

if ! curl -sf "http://127.0.0.1:$PORT/json/version" >/dev/null; then
  "$CHROME" --headless=new --remote-debugging-port="$PORT" --user-data-dir="$PROFILE" \
    --autoplay-policy=no-user-gesture-required --use-fake-ui-for-media-stream \
    --no-first-run --no-default-browser-check about:blank >/tmp/e2e-chrome.log 2>&1 &
  echo $! > "$PIDFILE"
  for _ in $(seq 50); do curl -sf "http://127.0.0.1:$PORT/json/version" >/dev/null && break; sleep 0.2; done
fi
curl -sf "http://127.0.0.1:$PORT/json/list" \
  | python3 -c 'import sys,json;print(next(t["webSocketDebuggerUrl"] for t in json.load(sys.stdin) if t["type"]=="page"))' \
  > "$CDP_FILE"
echo "CDP -> $(cat "$CDP_FILE")"
