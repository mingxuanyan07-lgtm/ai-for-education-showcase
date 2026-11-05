param([int]$Port = 8108)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$stateDir = Join-Path $projectRoot 'work\hybrid-tutor\console'
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) { throw "Port $Port is occupied; refusing to start or stop another service." }
$process = Start-Process -FilePath $python -ArgumentList @('-X','utf8','scripts/hybrid_tutor/server.py','--port',"$Port") -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $stateDir 'stdout.log') -RedirectStandardError (Join-Path $stateDir 'stderr.log')
$state = @{pid=$process.Id; started_utc=$process.StartTime.ToUniversalTime().ToString('o'); python=$python; script=(Join-Path $projectRoot 'scripts\hybrid_tutor\server.py'); port=$Port; url="http://127.0.0.1:$Port"}
$state | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stateDir 'process.json') -Encoding utf8
$state | ConvertTo-Json
