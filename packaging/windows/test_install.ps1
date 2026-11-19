# Installs CouchElephant silently on a Windows machine and checks what the
# installer promises. Run from the repo root after the installer is built:
#   powershell -File packaging\windows\test_install.ps1
$ErrorActionPreference = 'Stop'
$setup = (Get-ChildItem dist\CouchElephant-Setup-*.exe | Select-Object -First 1).FullName
$app = Join-Path $env:ProgramFiles 'CouchElephant'
$data = Join-Path $env:ProgramData 'CouchElephant'

function Check($ok, $what) {
    if (-not $ok) { throw "FAILED: $what" }
    Write-Host "ok: $what"
}

function Wait-Until($what, [scriptblock]$test, $seconds = 120) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        if (& $test) { return }
        Start-Sleep -Seconds 1
    }
    throw "TIMEOUT waiting for: $what"
}

function Hello($port) {
    try {
        return (Invoke-WebRequest -UseBasicParsing "http://localhost:$port/api/tv/hello" -TimeoutSec 5).StatusCode
    } catch { return 0 }
}

function Install-It {
    # Not -Wait: that waits for the tray the installer starts, which never ends.
    $p = Start-Process $setup -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=$env:TEMP\ce-setup.log" -PassThru
    if (-not $p.WaitForExit(300000)) { throw 'installer did not finish in 5 minutes' }
    Check ($p.ExitCode -eq 0) 'installer exits 0'
}

function Uninstall-It($extra) {
    $args = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') + $extra
    $p = Start-Process (Join-Path $app 'unins000.exe') -ArgumentList $args -PassThru
    $null = $p.WaitForExit(60000)
    # The uninstaller hands off to a temporary copy, so wait for it to finish.
    Wait-Until 'uninstall to finish' { -not (Test-Path (Join-Path $app 'unins000.exe')) }
    Wait-Until 'service to go' { -not (Get-Service CouchElephant -ErrorAction SilentlyContinue) }
}

try {
    # 1. The normal case.
    Install-It
    Wait-Until 'service to run' { (Get-Service CouchElephant).Status -eq 'Running' } 30
    Check $true 'service is running'
    Wait-Until 'hello on 8710' { (Hello 8710) -eq 200 }
    Check $true 'http://localhost:8710/api/tv/hello answers 200'
    $rule = Get-NetFirewallRule -DisplayName 'CouchElephant'
    Check ($null -ne $rule) 'firewall rule exists'
    Check ("$($rule.Profile)" -eq 'Private') "firewall rule is Private only (was $($rule.Profile))"
    $filter = $rule | Get-NetFirewallPortFilter
    Check ("$($filter.LocalPort)" -eq '8710') 'firewall rule covers port 8710'
    Check (Test-Path (Join-Path $data 'couchelephant.db')) 'database is in ProgramData'
    Check (Test-Path (Join-Path $data 'recordings')) 'recordings folder exists'

    # The data folder holds the Plex token and secrets: no ordinary user may read it.
    foreach ($dir in @($data, (Join-Path $data 'recordings'), (Join-Path $data 'couchelephant.db'))) {
        $open = (Get-Acl $dir).Access | Where-Object {
            $_.AccessControlType -eq 'Allow' -and
            "$($_.IdentityReference)" -match '^(BUILTIN\\Users|Everyone|NT AUTHORITY\\Authenticated Users)$' }
        Check ($null -eq $open) "no ordinary users in the ACL of $dir"
    }
    $svc = Get-CimInstance Win32_Service -Filter "Name='CouchElephant'"
    Check ($svc.StartName -eq 'NT AUTHORITY\LocalService') "service runs as LocalService (was $($svc.StartName))"
    $acl = (Get-Acl $data).Access | Where-Object { "$($_.IdentityReference)" -eq 'NT AUTHORITY\LOCAL SERVICE' }
    Check ($null -ne $acl -and "$($acl.FileSystemRights)" -match 'Modify') 'LocalService can modify the data folder'

    # Upgrade: install again over a running install. Data stays, service runs.
    Set-Content (Join-Path $data 'upgrade-marker.txt') 'kept'
    Install-It
    Wait-Until 'service to run after upgrade' { (Get-Service CouchElephant).Status -eq 'Running' } 30
    Wait-Until 'hello after upgrade' { (Hello 8710) -eq 200 }
    Check (Test-Path (Join-Path $data 'upgrade-marker.txt')) 'upgrade keeps the data'
    Check ((Get-CimInstance Win32_Service -Filter "Name='CouchElephant'").StartName -eq 'NT AUTHORITY\LocalService') 'upgrade keeps the service account'
    Check (Test-Path (Join-Path $app 'tray\CouchElephantTray.exe')) 'tray program installed'
    $run = Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run'
    Check ($null -ne $run.CouchElephantTray) 'tray starts at login'

    # 2. Uninstall keeps the data.
    Uninstall-It @()
    Check (Test-Path (Join-Path $data 'couchelephant.db')) 'uninstall keeps the data'
    Check (-not (Get-NetFirewallRule -DisplayName 'CouchElephant' -ErrorAction SilentlyContinue)) 'firewall rule removed'

    # 3. Port taken: the installer moves to 8711.
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, 8710)
    $listener.Start()
    try {
        Install-It
        Wait-Until 'hello on 8711' { (Hello 8711) -eq 200 }
        Check $true 'a taken 8710 moves the server to 8711'
        $filter = Get-NetFirewallRule -DisplayName 'CouchElephant' | Get-NetFirewallPortFilter
        Check ("$($filter.LocalPort)" -eq '8711') 'firewall rule follows to 8711'
    } finally {
        $listener.Stop()
    }

    # 4. /DELETEDATA=1 removes the data.
    Uninstall-It @('/DELETEDATA=1')
    Check (-not (Test-Path $data)) '/DELETEDATA=1 removes the data folder'
    Write-Host 'install test ok'
} catch {
    Write-Host $_
    # What a timeout needs to be explained: who runs, who holds the port, what the server said.
    Write-Host '--- service'
    Get-CimInstance Win32_Service -Filter "Name='CouchElephant'" | Format-List Name, State, ProcessId, StartName | Out-String | Write-Host
    Write-Host '--- couchelephant processes'
    Get-CimInstance Win32_Process -Filter "Name='couchelephant.exe' or Name='CouchElephantService.exe'" |
        Select-Object ProcessId, ParentProcessId, Name, CreationDate | Format-Table | Out-String | Write-Host
    Write-Host '--- listeners on 8710-8711'
    Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in 8710, 8711 } |
        ForEach-Object { "$($_.LocalPort) pid $($_.OwningProcess) $((Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName)" } | Write-Host
    Write-Host '--- who holds the database'
    Get-Process | Where-Object { $_.Path -and $_.Path -like "$app*" } | ForEach-Object { "$($_.Id) $($_.Path)" } | Write-Host
    if (Test-Path "$env:TEMP\ce-setup.log") { Get-Content "$env:TEMP\ce-setup.log" -Tail 60 }
    if (Test-Path (Join-Path $data 'logs')) { Get-ChildItem (Join-Path $data 'logs') | ForEach-Object { "--- $_"; Get-Content $_.FullName -Tail 80 } }
    exit 1
}
