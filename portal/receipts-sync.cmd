@echo off
rem Unscatter receipts sync - double-click to copy waiting receipts from Finance into your OneDrive.
rem Keep this file in the same folder as receipts-sync.ps1.
if not exist "%~dp0receipts-sync.ps1" (
  echo receipts-sync.ps1 was not found next to this file ^(%~dp0^).
  echo Download it again from Finance - Receipts - "Receipts sync on your computer" and save it in this folder.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0receipts-sync.ps1" %*
