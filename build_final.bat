@echo off
set PATH=%PATH:C:\Users\J03366\AppData\Local\Programs\Git\usr\bin;=%
set PATH=%PATH:C:\Users\J03366\.eide\bin\builder\msys\bin;=%

REM Use Rust's built-in LLD linker instead of MSVC link.exe
set CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER=rust-lld
set CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=rust-lld

%USERPROFILE%\.cargo\bin\rustup.exe default stable-x86_64-pc-windows-msvc
%USERPROFILE%\.cargo\bin\cargo.exe test --lib --manifest-path "D:/software/CC-Switch-src/src-tauri/Cargo.toml"
