@echo off
REM FanVPN Bridge - Native Messaging host launcher
REM Chrome launches this .bat via Native Messaging.
REM stdout/stdin are the NM channel - do NOT write anything else to stdout.
"C:\Users\J03366\AppData\Local\Programs\Python\Python314\python.exe" -u "D:\software\Note\fanvpn-bridge\native-host\bridge.py"
