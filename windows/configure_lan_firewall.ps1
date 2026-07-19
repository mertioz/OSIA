param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Install', 'Remove')]
    [string]$Action,

    [string]$ProgramPath = ''
)

$ErrorActionPreference = 'Stop'
$ruleName = 'OSIA Secure LAN Direct'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', ('"{0}"' -f $PSCommandPath),
        '-Action', $Action
    )
    if (-not [string]::IsNullOrWhiteSpace($ProgramPath)) {
        $arguments += @('-ProgramPath', ('"{0}"' -f $ProgramPath))
    }
    $elevated = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru `
        -ArgumentList $arguments
    exit $elevated.ExitCode
}

Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule

if ($Action -eq 'Remove') {
    Write-Host 'OSIA Private-network firewall rule removed.'
    exit 0
}

$targetProgram = $ProgramPath
if ([string]::IsNullOrWhiteSpace($targetProgram)) {
    $targetProgram = $env:OSIA_FIREWALL_PYTHON
}
if ([string]::IsNullOrWhiteSpace($targetProgram) -or -not (Test-Path -LiteralPath $targetProgram)) {
    throw 'The OSIA executable was not found.'
}

New-NetFirewallRule `
    -DisplayName $ruleName `
    -Description 'Allow authenticated OSIA LAN-direct TLS from the local private subnet.' `
    -Direction Inbound `
    -Action Allow `
    -Enabled True `
    -Profile Private `
    -Protocol TCP `
    -LocalPort 8766 `
    -RemoteAddress LocalSubnet `
    -Program ([IO.Path]::GetFullPath($targetProgram)) | Out-Null

$rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction Stop
if ($rule.Direction -ne 'Inbound' -or $rule.Action -ne 'Allow' -or
    $rule.Profile.ToString() -ne 'Private' -or -not $rule.Enabled) {
    throw 'The OSIA firewall rule failed its Private-profile policy check.'
}
$port = $rule | Get-NetFirewallPortFilter
if ($port.Protocol -ne 'TCP' -or $port.LocalPort -ne '8766') {
    throw 'The OSIA firewall rule failed its port policy check.'
}
$address = $rule | Get-NetFirewallAddressFilter
if ($address.RemoteAddress -notcontains 'LocalSubnet') {
    throw 'The OSIA firewall rule failed its local-subnet policy check.'
}

Write-Host 'OSIA LAN firewall rule installed for Private networks only.'
