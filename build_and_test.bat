@echo off
set PATH=%USERPROFILE%\.cargo\bin;%PATH%
REM Remove Git's link.exe from PATH to avoid conflict with GNU ld
set PATH=%PATH:C:\Users\J03366\AppData\Local\Programs\Git\usr\bin;=%
set PATH=%PATH:C:\Users\J03366\.eide\bin\builder\msys\bin;=%

rustup default stable-x86_64-pc-windows-gnu

cd /d D:\software\CC-Switch-src\src-tauri
rmdir /s /q target 2>nul

echo === Building tests (GNU toolchain) ===
cargo test --lib --no-run --target x86_64-pc-windows-gnu 2>&1
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

echo.
echo === Running transform_gemini tests ===
cargo test --lib --target x86_64-pc-windows-gnu proxy::providers::transform_gemini::tests 2>&1
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

echo.
echo === Running streaming_gemini tests ===
cargo test --lib --target x86_64-pc-windows-gnu proxy::providers::streaming_gemini::tests 2>&1
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

echo.
echo === ALL TESTS PASSED ===
