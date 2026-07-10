"""Restart the bridge server."""
import subprocess
import sys
import os

# Kill any existing server
try:
    subprocess.run([
        sys.executable, '-c',
        'import socket; s=socket.socket(); '
        's.settimeout(1); '
        'r=s.connect_ex(("127.0.0.1",18888)); '
        's.close(); '
        'print(f"Port 18888: {\"in use\" if r==0 else \"free\"}")'
    ], check=True)
except Exception:
    pass

# Print target config
import json
config_path = os.path.join(os.path.dirname(__file__), 'config.json')
with open(config_path) as f:
    config = json.load(f)
print(f"Target: {config['target_base_url']}")

# Start server
print("Starting server...")
# Just run it directly
os.chdir(os.path.dirname(__file__))
os.execv(sys.executable, [sys.executable, '-u', 'bridge.py'])
