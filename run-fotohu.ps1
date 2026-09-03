# Wrapper for Task Scheduler. The bot writes its own log to data\bot.log
# (FOTOHU_LOG_FILE in .env); this only ensures the right working directory.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\pythonw.exe" -m fotohu
