# The portable release already contains Ollama and qwen3:4b; no downloads are used.
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'start.ps1')
