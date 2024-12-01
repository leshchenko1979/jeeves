# PowerShell Make Script for Jeeves Project

# Stop on any error
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

Write-Host "Starting deployment..." -ForegroundColor Green

function Get-EnvVariables {
    Write-Host "Reading environment variables..." -ForegroundColor Green
    $envVars = @{}
    if (Test-Path .env) {
        Get-Content .env | ForEach-Object {
            if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
                $name = $matches[1].Trim()
                $value = $matches[2].Trim()
                $envVars[$name] = $value
            }
        }
    } else {
        Write-Host "Warning: .env file not found!" -ForegroundColor Yellow
        exit 1
    }
    return $envVars
}

function Install-LocalDependencies {
    Write-Host "Installing Python dependencies locally..." -ForegroundColor Green

    if (-not (Test-Path venv)) {
        Write-Host "Creating new virtual environment..." -ForegroundColor Green
        python -m venv venv
    }

    # Install all requirements except tgcrypto
    Get-Content requirements.txt | Where-Object { $_ -notmatch 'tgcrypto' } | Set-Content requirements.local.txt
    .\venv\Scripts\pip install -r requirements.local.txt
    Remove-Item requirements.local.txt

    # Install development dependencies
    .\venv\Scripts\pip install pytest pytest-asyncio isort black

    Write-Host "Dependencies installed successfully!" -ForegroundColor Green
}

function Install-RemoteDependencies {
    param (
        [Parameter(Mandatory=$true)]
        [string]$RemoteHost,
        [Parameter(Mandatory=$true)]
        [string]$RemoteUser
    )

    Write-Host "Installing Python dependencies on remote host..." -ForegroundColor Green

    $commands = @(
        'cd /home/jeeves',
        'sudo -u jeeves python3 -m venv venv',
        'sudo -u jeeves venv/bin/pip install -r requirements.txt'
    ) -join ' && '

    ssh "${RemoteUser}@${RemoteHost}" $commands
}

function Format-Code {
    Write-Host "Formatting code..." -ForegroundColor Green
    .\venv\Scripts\isort jeeves tests
    .\venv\Scripts\black jeeves tests
}

function Run-Tests {
    Write-Host "Installing dependencies and running tests..." -ForegroundColor Green
    Install-LocalDependencies
    Format-Code
    .\venv\Scripts\python -m pytest -x --ff
}

function Setup-Logs {
    param (
        [Parameter(Mandatory=$true)]
        [string]$RemoteHost,
        [Parameter(Mandatory=$true)]
        [string]$RemoteUser
    )

    Write-Host "Setting up log directory on remote host..." -ForegroundColor Green

    $commands = @(
        'sudo mkdir -p /var/log/jeeves',
        'sudo chown -R jeeves:jeeves /var/log/jeeves',
        'sudo chmod -R 755 /var/log/jeeves',
        'sudo touch /var/log/jeeves/app.log /var/log/jeeves/error.log',
        'sudo chown jeeves:jeeves /var/log/jeeves/app.log /var/log/jeeves/error.log'
    ) -join ' && '

    ssh "${RemoteUser}@${RemoteHost}" $commands
}

function Deploy-Files {
    param (
        [Parameter(Mandatory=$true)]
        [string]$RemoteHost,
        [Parameter(Mandatory=$true)]
        [string]$RemoteUser
    )

    Write-Host "Deploying application files..." -ForegroundColor Green

    # Create remote directory
    ssh "${RemoteUser}@${RemoteHost}" 'sudo mkdir -p /home/jeeves/jeeves'

    # Clean up __pycache__ directories locally
    Get-ChildItem -Path "./jeeves" -Filter "__pycache__" -Recurse | Remove-Item -Recurse -Force

    # Create and prepare temp directory
    $tempDir = Join-Path $env:TEMP "ai_sales_deploy"
    Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $tempDir | Out-Null

    # Copy files excluding tests
    Get-ChildItem -Path "./jeeves" -Recurse |
        Where-Object {
            -not $_.PSIsContainer -and
            -not $_.Name.EndsWith("_test.py") -and
            -not $_.FullName.Contains("tests") -and
            -not $_.FullName.Contains("script_tests")
        } |
        ForEach-Object {
            $relativePath = $_.FullName.Substring((Get-Location).Path.Length + 1)
            $targetPath = Join-Path $tempDir $relativePath
            $targetDir = Split-Path -Parent $targetPath
            if (-not (Test-Path $targetDir)) {
                New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
            }
            Copy-Item $_.FullName -Destination $targetPath
        }

    # Deploy files
    Write-Host "`nCopying files to server..." -ForegroundColor Green
    scp requirements.txt "${RemoteUser}@${RemoteHost}:/home/jeeves/"
    scp -r "${tempDir}/*" "${RemoteUser}@${RemoteHost}:/home/jeeves/"

    # Set permissions
    ssh "${RemoteUser}@${RemoteHost}" 'sudo chown -R jeeves:jeeves /home/jeeves'
}

function Configure-Service {
    param (
        [Parameter(Mandatory=$true)]
        [string]$RemoteHost,
        [Parameter(Mandatory=$true)]
        [string]$RemoteUser
    )

    Write-Host "Configuring systemd service..." -ForegroundColor Green

    $envVars = Get-EnvVariables
    $envLines = $envVars.GetEnumerator() | ForEach-Object {
        "Environment=`"$($_.Key)=$($_.Value)`""
    }
    $envSection = [System.String]::Join("`n", $envLines)

    $serviceContent = @"
[Unit]
Description=Sales Bot
After=network.target postgresql.service

[Service]
Type=simple
User=jeeves
WorkingDirectory=/home/jeeves/jeeves
Environment=PYTHONPATH=/home/jeeves
$envSection
ExecStart=/home/jeeves/venv/bin/python -u main.py
StandardOutput=append:/var/log/jeeves/app.log
StandardError=append:/var/log/jeeves/error.log
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"@

    # Convert to Unix line endings
    $serviceContent = $serviceContent.Replace("`r`n", "`n")

    $serviceContent | ssh "${RemoteUser}@${RemoteHost}" 'sudo tee /etc/systemd/system/jeeves.service'
}

function Start-Service {
    param (
        [Parameter(Mandatory=$true)]
        [string]$RemoteHost,
        [Parameter(Mandatory=$true)]
        [string]$RemoteUser
    )

    Write-Host "Starting and verifying service..." -ForegroundColor Green

    # Use single command string with semicolons
    $commands = @(
        'sudo systemctl daemon-reload',
        'sudo systemctl enable jeeves',
        'sudo systemctl restart jeeves',
        'sleep 5',
        'sudo systemctl status jeeves',
        'sudo tail -n 20 /var/log/jeeves/app.log'
    ) -join '; '

    ssh "${RemoteUser}@${RemoteHost}" $commands
}

function Deploy-All {
    param (
        [Parameter(Mandatory=$true)]
        [string]$RemoteHost,
        [Parameter(Mandatory=$true)]
        [string]$RemoteUser
    )

    $startTime = Get-Date
    Write-Host "Starting deployment at $startTime" -ForegroundColor Green

    Run-Tests
    Setup-Logs -RemoteHost $RemoteHost -RemoteUser $RemoteUser
    Deploy-Files -RemoteHost $RemoteHost -RemoteUser $RemoteUser
    Install-RemoteDependencies -RemoteHost $RemoteHost -RemoteUser $RemoteUser
    Configure-Service -RemoteHost $RemoteHost -RemoteUser $RemoteUser
    Start-Service -RemoteHost $RemoteHost -RemoteUser $RemoteUser

    $endTime = Get-Date
    $duration = $endTime - $startTime
    Write-Host "`nDeployment completed successfully in $($duration.TotalMinutes) minutes" -ForegroundColor Green
}

# Main execution
$envVars = Get-EnvVariables
Deploy-All -RemoteHost $envVars['REMOTE_HOST'] -RemoteUser $envVars['REMOTE_USER']
