@echo off
set PATH=%USERPROFILE%\.cargo\bin;%PATH%
cd /d D:\software\CC-Switch-src\src-tauri
echo === Running transform_gemini tests ===
cargo test --lib proxy::providers::transform_gemini::tests 2>&1
echo.
echo === Running streaming_gemini tests ===
cargo test --lib proxy::providers::streaming_gemini::tests 2>&1
echo.
echo === Done ===
