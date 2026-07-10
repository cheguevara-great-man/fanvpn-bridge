@echo off
set KEY_FILE=%TEMP%\fanvpn-bridge-logs\_key.txt
set /p API_KEY=<%KEY_FILE%
python "D:\software\Note\fanvpn-bridge\native-host\test_bridge.py" "%API_KEY%"
