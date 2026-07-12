"""PyInstaller entry point kept outside the importable package."""

from fanvpn_bridge.main import main


if __name__ == "__main__":
    raise SystemExit(main())
