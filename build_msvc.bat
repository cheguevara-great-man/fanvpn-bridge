@echo off
REM Remove Git's broken link.exe from PATH so Rust uses its own linker
set PATH=%PATH:C:\Users\J03366\AppData\Local\Programs\Git\usr\bin;=%
set PATH=%PATH:C:\Users\J03366\.eide\bin\builder\msys\bin;=%

set RUSTFLAGS=-C link-arg=-fuse-ld=lld

%USERPROFILE%\.cargo\bin\cargo.exe clean --manifest-path "D:/software/CC-Switch-src/src-tauri/Cargo.toml"
%USERPROFILE%\.cargo\bin\cargo.exe test --lib --manifest-path "D:/software/CC-Switch-src/src-tauri/Cargo.toml"
