@echo off
set PATH=%PATH:C:\Users\J03366\AppData\Local\Programs\Git\usr\bin;=%
set PATH=%PATH:C:\Users\J03366\.eide\bin\builder\msys\bin;=%

REM Use Rust's built-in LLD linker instead of MSVC link.exe
set RUSTFLAGS=-C linker=rust-lld

%USERPROFILE%\.cargo\bin\cargo.exe test --lib --manifest-path "D:/software/CC-Switch-src/src-tauri/Cargo.toml"
