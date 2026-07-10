# Install Rust and run CC Switch tests
Write-Host "Installing Rust..." -ForegroundColor Cyan
winget install Rustlang.Rustup --accept-package-agreements --accept-source-agreements 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "winget failed, trying direct download..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri "https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe" -OutFile "$env:TEMP\rustup-init.exe"
    & "$env:TEMP\rustup-init.exe" -y
}
Write-Host "Done. Restart your terminal and run: cargo test" -ForegroundColor Green
