param(
  [string]$Store = "$PSScriptRoot\.autoresearch",
  [int]$ApiPort = 8090,
  [int]$UiPort = 5173
)

$env:PYTHONPATH = "$PSScriptRoot\src"
# A new PowerShell process does not necessarily refresh User environment
# variables set after the desktop app started. Load the persisted database
# setting explicitly so the API cannot silently fall back to local JSON.
if (-not $env:AUTORESEARCH_DATABASE_URL) {
  $env:AUTORESEARCH_DATABASE_URL = [Environment]::GetEnvironmentVariable('AUTORESEARCH_DATABASE_URL', 'User')
}
Start-Process -FilePath python -ArgumentList "-m autoresearch.api --store `"$Store`" --host 127.0.0.1 --port $ApiPort" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
Start-Process -FilePath python -ArgumentList "-m http.server $UiPort --bind 127.0.0.1 --directory `"$PSScriptRoot\frontend`"" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
Write-Host "AutoResearch UI:  http://127.0.0.1:$UiPort"
Write-Host "AutoResearch API: http://127.0.0.1:$ApiPort"
