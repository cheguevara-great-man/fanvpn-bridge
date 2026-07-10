"""
End-to-end test: restart server, wait for bridge, send request.
Run: python test_bridge.py [API_KEY]
"""
import subprocess, socket, time, sys, os, json

TEMP = os.environ.get("TEMP", os.path.expanduser("~"))
API_KEY = sys.argv[1] if len(sys.argv) > 1 else None
if not API_KEY:
    key_file = os.path.join(TEMP, "fanvpn-bridge-logs", "_key.txt")
    try:
        with open(key_file) as f:
            API_KEY = f.read().strip()
    except Exception:
        pass
if not API_KEY:
    API_KEY = "test-key"
    print("[warn] No API key found, using 'test-key'")
LOG_DIR = os.path.join(TEMP, "fanvpn-bridge-logs")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# --- 1. Show config ---
config_path = os.path.join(SCRIPT_DIR, "config.json")
with open(config_path) as f:
    config = json.load(f)
print(f"[config] target: {config['target_base_url']}")
print(f"[config] strip:  {config.get('strip_path_prefix', '(none)')}")

# --- 2. Kill old server ---
print("[kill] looking for old server...")
r = subprocess.run("netstat -ano", shell=True, capture_output=True, text=True).stdout
for line in r.splitlines():
    if "127.0.0.1:18888" in line and "LISTENING" in line:
        pid = line.strip().split()[-1]
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        print(f"[kill] killed PID {pid}")

time.sleep(1.5)

# --- 3. Start new server ---
print("[start] launching server...")
os.makedirs(LOG_DIR, exist_ok=True)
log_path = os.path.join(LOG_DIR, "server.log")
with open(log_path, "w") as f:
    f.write("")

os.chdir(SCRIPT_DIR)
p = subprocess.Popen(
    [sys.executable, "-u", "bridge.py"],
    stdout=open(log_path, "a"),
    stderr=subprocess.STDOUT,
    cwd=SCRIPT_DIR,
)
print(f"[start] server PID={p.pid}")

# --- 4. Wait for bridge ---
print("[wait] waiting for Chrome bridge...")
for i in range(20):
    time.sleep(1)
    try:
        with open(log_path) as f:
            if "Bridge connected" in f.read():
                print(f"[wait] Bridge connected! ({i+1}s)")
                break
    except Exception:
        pass
else:
    print("[wait] Bridge not connected after 20s, testing anyway...")

# --- 5. Test: list models ---
print(f"[test] GET /v1/models (Gemini OpenAI compat)...")
s = socket.socket()
s.settimeout(30)
try:
    s.connect(("127.0.0.1", 18888))
    auth_line = f"Authorization: Bearer {API_KEY}\r\n".encode()
    req = (
        b"GET /v1/models HTTP/1.1\r\n"
        b"Host: 127.0.0.1:18888\r\n"
        + auth_line +
        b"Connection: close\r\n"
        b"\r\n"
    )
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
        headers, body = text.split("\r\n\r\n", 1)
        status_line = headers.split("\r\n")[0] if headers else ""
        print(f"\n[response] STATUS: {status_line}")

        # Show key headers
        for h in headers.split("\r\n"):
            hl = h.lower()
            if any(k in hl for k in ("content-type", "cf-ray", "server", "date", "www-authenticate")):
                print(f"  {h}")

        body = body.strip()
        try:
            data = json.loads(body)
            print(f"\n[body] ({len(json.dumps(data))} chars)")
            if "data" in data:
                print(f"  Models: {len(data['data'])}")
                for m in data["data"][:5]:
                    print(f"    {m.get('id', '?')}")
            elif "error" in data:
                print(f"  Error: {json.dumps(data, indent=2)[:400]}")
            else:
                print(json.dumps(data, indent=2)[:400])
        except json.JSONDecodeError:
            print(f"  Raw: {body[:400]}")
    else:
        print(f"[response] RAW: {text[:400]}")

    # --- 6. Test: chat completion ---
    print(f"\n[test] POST /v1/chat/completions (stream test)...")
    s2 = socket.socket()
    s2.settimeout(30)
    s2.connect(("127.0.0.1", 18888))
    body = json.dumps({
        "model": "gemini-2.5-flash",
        "messages": [{"role": "user", "content": "Say hello in one word."}],
        "stream": True,
    })
    auth_line2 = f"Authorization: Bearer {API_KEY}\r\n".encode()
    cl_line = f"Content-Length: {len(body)}\r\n".encode()
    req2 = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: 127.0.0.1:18888\r\n"
        b"Content-Type: application/json\r\n"
        + auth_line2 +
        cl_line +
        b"Connection: close\r\n"
        b"\r\n"
        + body.encode()
    )
    s2.sendall(req2)

    resp2 = b""
    while True:
        try:
            chunk = s2.recv(65536)
            if not chunk:
                break
            resp2 += chunk
        except socket.timeout:
            break
    s2.close()

    text2 = resp2.decode("utf-8", errors="replace")
    if "\r\n\r\n" in text2:
        headers2, body2 = text2.split("\r\n\r\n", 1)
        status_line2 = headers2.split("\r\n")[0] if headers2 else ""
        print(f"  STATUS: {status_line2}")
        print(f"  BODY: {body2[:600]}")
    else:
        print(f"  RAW: {text2[:400]}")

    # --- 7. Server log ---
    print(f"\n[server log]")
    try:
        with open(log_path) as f:
            for line in f.readlines()[-8:]:
                print(f"  {line.rstrip()}")
    except Exception:
        pass

except Exception as e:
    print(f"[FAIL] {e}")
    import traceback
    traceback.print_exc()

print("\nDone.")
