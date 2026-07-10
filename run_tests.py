"""Run CC Switch tests and capture output."""
import subprocess, os, sys

os.environ["PATH"] = os.path.expanduser("~/.cargo/bin") + ";" + os.environ.get("PATH", "")

tests = [
    ("transform_gemini", "proxy::providers::transform_gemini::tests"),
    ("streaming_gemini", "proxy::providers::streaming_gemini::tests"),
    ("gemini_shadow", "proxy::providers::gemini_shadow::tests"),
]

for name, filter_str in tests:
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    r = subprocess.run(
        ["cargo", "test", "--lib", filter_str],
        cwd="D:/software/CC-Switch-src/src-tauri",
        capture_output=True, text=True, timeout=600
    )
    # Print last 60 lines of output
    lines = (r.stdout + r.stderr).splitlines()
    for line in lines[-60:]:
        print(line)
    if r.returncode != 0:
        print(f"\n!!! FAILED (exit {r.returncode})")
        sys.exit(1)

print("\n" + "="*60)
print("  ALL TESTS PASSED")
print("="*60)
