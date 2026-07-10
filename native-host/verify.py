"""
Systematic bridge verification:
  1. /health        — check bridge connectivity
  2. /v1/models     — list models
  3. /v1/chat/completions (non-streaming) — basic chat
  4. /v1/chat/completions (streaming)     — SSE streaming

Run: python verify.py
"""
import subprocess, socket, time, sys, os, json

TEMP = os.environ.get("TEMP", os.path.expanduser("~"))
LOG_DIR = os.path.join(TEMP, "fanvpn-bridge-logs")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(LOG_DIR, "server.log")

# Read API key from temp file
API_KEY = "test-key"
key_file = os.path.join(LOG_DIR, "_key.txt")
try:
    with open(key_file) as f:
        API_KEY = f.read().strip()
except Exception:
    pass


def kill_old_server():
    r = subprocess.run("netstat -ano", shell=True, capture_output=True, text=True).stdout
    for line in r.splitlines():
        if "127.0.0.1:18888" in line and "LISTENING" in line:
            pid = line.strip().split()[-1]
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
            print(f"  killed old server PID {pid}")


def start_server():
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_PATH, "w") as f:
        f.write("")
    os.chdir(SCRIPT_DIR)
    p = subprocess.Popen(
        [sys.executable, "-u", "bridge.py"],
        stdout=open(LOG_PATH, "a"),
        stderr=subprocess.STDOUT,
        cwd=SCRIPT_DIR,
    )
    print(f"  server PID={p.pid}")
    return p


def wait_for_bridge(timeout=20):
    for i in range(timeout):
        time.sleep(1)
        try:
            with open(LOG_PATH) as f:
                if "Bridge connection established" in f.read():
                    print(f"  bridge connected ({i + 1}s)")
                    return True
        except Exception:
            pass
    print(f"  WARNING: bridge not connected after {timeout}s")
    return False


def http_request(method, path, body=None, timeout=30):
    """Send HTTP request, return (status_code, headers_dict, body_bytes)."""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", 18888))
        req = f"{method} {path} HTTP/1.1\r\n"
        req += "Host: 127.0.0.1:18888\r\n"
        if body:
            req += f"Content-Length: {len(body)}\r\n"
            req += "Content-Type: application/json\r\n"
        req += f"Authorization: Bearer {API_KEY}\r\n"
        req += "Connection: close\r\n"
        req += "\r\n"
        if body:
            req = req.encode() + body.encode() if isinstance(body, str) else req.encode() + body
        else:
            req = req.encode()

        s.sendall(req)
        resp = b""
        while True:
            try:
                chunk = s.recv(65536)
                if not chunk:
                    break
                resp += chunk
            except socket.timeout:
                break
        s.close()

        text = resp.decode("utf-8", errors="replace")
        if "\r\n\r\n" in text:
            header_text, body_text = text.split("\r\n\r\n", 1)
            status_line = header_text.split("\r\n")[0]
            try:
                status_code = int(status_line.split(" ")[1])
            except (IndexError, ValueError):
                status_code = 0
            headers = {}
            for line in header_text.split("\r\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            return status_code, headers, body_text.encode("utf-8") if isinstance(body_text, str) else body_text
        else:
            return 0, {}, text.encode()
    except Exception as e:
        return 0, {}, str(e).encode()


def show_log(n=10):
    try:
        with open(LOG_PATH) as f:
            lines = f.readlines()
            for line in lines[-n:]:
                print(f"    {line.rstrip()}")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
print("=" * 55)
print("  FanVPN Bridge — Systematic Verification")
print("=" * 55)

# ── Setup ─────────────────────────────────────────────────────────────
print("\n[1/5] Starting server...")
kill_old_server()
time.sleep(1)
server = start_server()
wait_for_bridge(timeout=15)

# ── Health ─────────────────────────────────────────────────────────────
print("\n[2/5] GET /health ...")
status, headers, body = http_request("GET", "/health")
print(f"  HTTP {status}")
try:
    health = json.loads(body)
    print(f"  http_server:              {health.get('http_server', '?')}")
    print(f"  bridge_listener:          {health.get('bridge_listener', '?')}")
    print(f"  native_bridge_connected:  {health.get('native_bridge_connected', '?')}")
    print(f"  pending_requests:         {health.get('pending_requests', '?')}")
    if not health.get("native_bridge_connected"):
        print("\n  *** BRIDGE NOT CONNECTED — cannot continue ***")
        print("  Check: Chrome running? Extension loaded? NM registered?")
        show_log(15)
        sys.exit(1)
except json.JSONDecodeError:
    print(f"  RAW: {body[:200]}")

# ── Models ─────────────────────────────────────────────────────────────
print("\n[3/5] GET /v1/models ...")
status, headers, body = http_request("GET", "/v1/models", timeout=20)
print(f"  HTTP {status}")
try:
    data = json.loads(body)
    if "data" in data:
        print(f"  Models: {len(data['data'])}")
        for m in data["data"][:5]:
            print(f"    - {m.get('id', '?')}")
    elif "error" in data:
        print(f"  Error: {json.dumps(data['error'], indent=4)[:300]}")
    else:
        print(f"  Response: {json.dumps(data, indent=2)[:300]}")
except json.JSONDecodeError:
    print(f"  RAW: {body[:300]}")

# ── Chat (non-streaming) ───────────────────────────────────────────────
print("\n[4/5] POST /v1/chat/completions (non-streaming)...")
req_body = json.dumps({
    "model": "gemini-2.5-flash",
    "messages": [{"role": "user", "content": "Say hello in one word."}],
    "stream": False,
})
status, headers, body = http_request("POST", "/v1/chat/completions", body=req_body, timeout=30)
print(f"  HTTP {status}")
try:
    data = json.loads(body)
    if "choices" in data:
        msg = data["choices"][0].get("message", {})
        print(f"  Response: {msg.get('content', '?')}")
        print(f"  Model:    {data.get('model', '?')}")
        usage = data.get("usage", {})
        if usage:
            print(f"  Tokens:   {usage.get('total_tokens', '?')}")
    elif "error" in data:
        print(f"  Error: {json.dumps(data['error'], indent=4)[:300]}")
    else:
        print(f"  Response: {json.dumps(data, indent=2)[:300]}")
except json.JSONDecodeError:
    print(f"  RAW: {body[:300]}")

# ── Chat (streaming) ───────────────────────────────────────────────────
print("\n[5/5] POST /v1/chat/completions (streaming)...")
req_body = json.dumps({
    "model": "gemini-2.5-flash",
    "messages": [{"role": "user", "content": "Count from 1 to 5, one per line."}],
    "stream": True,
})
status, headers, body = http_request("POST", "/v1/chat/completions", body=req_body, timeout=30)
print(f"  HTTP {status}")
ct = headers.get("content-type", "")
print(f"  Content-Type: {ct}")
body_text = body.decode("utf-8", errors="replace")
if "text/event-stream" in ct:
    # Parse SSE
    lines = body_text.strip().split("\n")
    events = [l for l in lines if l.startswith("data:")]
    print(f"  SSE events: {len(events)}")
    for ev in events[:5]:
        try:
            d = json.loads(ev[6:])
            choices = d.get("choices", [{}])
            delta = choices[0].get("delta", {})
            content = delta.get("content", "")
            if content:
                print(f"    {repr(content)}")
        except Exception:
            print(f"    {ev[:80]}")
    if len(events) > 5:
        print(f"    ... ({len(events) - 5} more)")
else:
    print(f"  RAW: {body_text[:300]}")

# ── Done ───────────────────────────────────────────────────────────────
print(f"\n{'=' * 55}")
print("  Server log (last lines):")
show_log(15)
print(f"{'=' * 55}")
print("Done.")
