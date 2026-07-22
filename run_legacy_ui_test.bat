@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%CD%\src"
set "ZAPRET_HUB_LEGACY_UI=1"
python -m zapret_hub.main
endlocal
