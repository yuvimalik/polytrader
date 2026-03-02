# Polytrader bot launcher (PowerShell)
# Paper trading by default. For live: .\run_bot.ps1 -Live

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

if (Test-Path .env) {
    Get-Content .env | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), 'Process')
        }
    }
}

$Args = $args
if ($Args.Count -eq 0) {
    $Args = @('--dry-run')
}

Write-Host "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK')] Starting polytrader bot..."
while ($true) {
    python polymarket_btc_bot.py @Args
    $ExitCode = $LASTEXITCODE
    Write-Host "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK')] Bot exited with code $ExitCode"
    if ($ExitCode -eq 0) {
        Write-Host "Clean exit -- stopping"
        break
    }
    Write-Host "Restarting in 10 seconds..."
    Start-Sleep -Seconds 10
}
