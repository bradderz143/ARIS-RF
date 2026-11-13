#Requires -Version 5.1

<#
.SYNOPSIS
Finds the radio receivers plugged into this Windows computer and attaches them to WSL, where ARIS runs.

.DESCRIPTION
ARIS runs inside WSL 2 (Linux on Windows). A USB receiver plugged into Windows cannot be seen there until the
program usbipd-win hands it over. This script finds the receivers it knows by their USB ID and does that.

With no switch it only LISTS what it found and what it would do. It changes NOTHING.

  -Attach   shares each receiver and attaches it to WSL
  -Detach   gives the receivers back to Windows
  -Auto     attaches them and attaches them again after a replug; keeps running until you press Ctrl+C
  -BusId    also handles a USB device this script does not know

Sharing a receiver ("usbipd bind") is done once per receiver, is remembered by Windows, and needs a PowerShell
window opened with "Run as administrator". Attaching it ("usbipd attach --wsl") needs no administrator, but has to
be done again after the receiver was unplugged, after Windows restarted and after WSL was shut down.

While a receiver is attached to WSL, Windows programs cannot use it. -Detach gives it back.

The script only touches the receivers in its list (RTL-SDR dongles, HackRF, Airspy, SDRplay, LimeSDR-USB,
ADALM-Pluto, USRP B2xx, bladeRF) and the devices you name with -BusId. Every other USB device is left alone.
Nothing here transmits, and nothing is installed or downloaded.

It needs usbipd-win 4.0.0 or newer (winget install --interactive --exact dorssel.usbipd-win; save your work
first, its installer may have to restart Windows) and a running WSL 2 distribution (open Ubuntu from the Start
menu and leave its window open).

A receiver that is not shared yet is shared by an administrator, once. If your administrator has allowed it
with a usbipd policy rule instead (usbipd policy add), this script still asks for the sharing step: attach
such a receiver by hand with  usbipd attach --wsl --busid <id>.

.PARAMETER Attach
Share the receivers (first time only, needs an administrator window) and attach them to WSL.

.PARAMETER Detach
Detach the receivers from WSL, so Windows can use them again. They stay shared, so the next -Attach needs no
administrator. Stop a running -Auto window first, or it attaches them again.

.PARAMETER Auto
Like -Attach, and then stays running: usbipd attaches each receiver again whenever it was unplugged and plugged
back into the same USB port. Leave the window open. Press Ctrl+C to stop. It ends when WSL is shut down.

.PARAMETER Unshare
Only with -Detach: also stop sharing the receivers ("usbipd unbind"). Needs an administrator window.

.PARAMETER BusId
The bus id of a USB device that is not in this script's list, as the list shows it (for example 2-3). Several:
-BusId 2-3,2-4. The devices named are handled in addition to the receivers the script knows.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\aris-usb.ps1

Lists the receivers found and what -Attach would do. Changes nothing.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\aris-usb.ps1 -Attach

Shares (first time: run it in an administrator window) and attaches every receiver found.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\aris-usb.ps1 -Auto

Attaches every receiver found and keeps doing so after a replug, until Ctrl+C.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\aris-usb.ps1 -Attach -BusId 3-1

Also attaches the USB device at bus id 3-1, which the script does not know.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\aris-usb.ps1 -Detach

Gives the receivers back to Windows.

.NOTES
Written for Windows PowerShell 5.1 and for PowerShell 7: it uses nothing that only PowerShell 7 has. Plain ASCII
on purpose: Windows PowerShell 5.1 reads a script file without a byte-order mark in the computer's legacy code
page.

Exit code: 0 = done (or, with no switch, listed), 1 = something is marked FIX, 2 = the switches were wrong.

Status: written from the usbipd-win and WSL documentation and source code, and NOT yet run on a Windows
computer. It was checked with a parser and by reading only. docs/WINDOWS.md says what is still unverified.

.LINK
https://github.com/dorssel/usbipd-win

.LINK
https://learn.microsoft.com/windows/wsl/connect-usb
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$Attach,
    [switch]$Detach,
    [switch]$Auto,
    [switch]$Unshare,
    [string[]]$BusId = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# PowerShell 7.3 and newer can turn a program's non-zero exit code into an error. This script reads
# $LASTEXITCODE itself after every program it runs, so that stays off here.
$PSNativeCommandUseErrorActionPreference = $false

# The receivers this script handles without being told, by USB vendor:product ID. Sources:
#   0bda:....  the rtl-sdr project's udev rules (rtl-sdr.rules)
#   1d50:6089, 1d50:604b, 1d50:cc15  the HackRF project's udev rules (53-hackrf.rules)
#   1d50:60a1  the Airspy host tools' udev rules (52-airspy.rules)
#   03eb:800c  the Airspy HF+ library's udev rules (52-airspyhf.rules)
#   1df7:....  the USB ID list of linux-usb.org (usb.ids); 2500, 3000 and 3010 also in libmirisdr's udev rules
#   1d50:6108  LimeSuite's udev rules (64-limesuite.rules)
#   0456:b673  the PlutoSDR firmware project's udev rules (53-adi-plutosdr-usb.rules)
#   2500:....  the UHD project's udev rules (uhd-usrp.rules), names from usb.ids
#   2cf0:....  the bladeRF project's udev rules (88-nuand-bladerf*.rules)
# Left out on purpose: IDs that belong to a general-purpose chip and not to one receiver, such as the
# LimeSDR Mini's 0403:601f (any device built on that FTDI chip has it). Name those with -BusId.
$KnownReceivers = @{
    '0bda:2838' = 'RTL-SDR dongle (RTL2832U)'
    '0bda:2832' = 'RTL-SDR dongle (RTL2832U)'
    '1d50:6089' = 'HackRF One'
    '1d50:604b' = 'HackRF Jawbreaker'
    '1d50:cc15' = 'rad1o badge (HackRF design)'
    '1d50:60a1' = 'Airspy'
    '03eb:800c' = 'Airspy HF+'
    '1df7:2500' = 'SDRplay RSP1'
    '1df7:3000' = 'SDRplay RSP1A'
    '1df7:3010' = 'SDRplay RSP2 / RSP2pro'
    '1df7:3020' = 'SDRplay RSPduo'
    '1df7:3030' = 'SDRplay RSPdx'
    '1d50:6108' = 'LimeSDR-USB'
    '0456:b673' = 'ADALM-Pluto (PlutoSDR)'
    '2500:0020' = 'USRP B200 / B210'
    '2500:0021' = 'USRP B200mini'
    '2500:0022' = 'USRP B205mini'
    '2cf0:5246' = 'bladeRF'
    '2cf0:5250' = 'bladeRF 2.0 micro'
}

$script:Problems = 0

function Write-Ok {
    param([string]$Text)
    Write-Host '  OK   ' -ForegroundColor Green -NoNewline
    Write-Host $Text
}

function Write-Note {
    param([string]$Text)
    Write-Host '  NOTE ' -ForegroundColor Yellow -NoNewline
    Write-Host $Text
}

function Write-Fix {
    param([string]$Text)
    Write-Host '  FIX  ' -ForegroundColor Red -NoNewline
    Write-Host $Text
    $script:Problems = $script:Problems + 1
}

function Write-More {
    # A second line under an OK / NOTE / FIX line.
    param([string]$Text)
    Write-Host ('       ' + $Text)
}

function Get-Prop {
    # A property of an object read from JSON, or nothing when it is not there (strict mode would stop
    # the script on a missing property).
    param($Object, [string]$Name)
    if ($null -eq $Object) {
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Invoke-Tool {
    # Runs a program and returns its exit code and what it printed, one string per line.
    # usbipd writes its messages to the error stream. In Windows PowerShell 5.1 a redirected error line
    # becomes an error record, which would stop a script that runs with 'Stop'. So this function, and only
    # this function, runs with 'Continue' (the setting is local to it).
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string[]]$Arguments = @(),
        [switch]$DropErrorText
    )
    $ErrorActionPreference = 'Continue'
    # usbipd writes UTF-8 whatever the console's code page is, and a device name can hold any letter.
    # PowerShell reads a program's text in the code page it found when it started. So say UTF-8 for the
    # time of the call and put the old setting back. (wsl.exe's own text is UTF-16; its plain letters
    # read the same either way.)
    $before = $null
    try {
        $before = [Console]::OutputEncoding
        [Console]::OutputEncoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
    }
    catch {
        # No console to set: read the program's text as before.
        $before = $null
    }
    $global:LASTEXITCODE = 0
    if ($DropErrorText) {
        $raw = & $Path @Arguments 2>$null
    }
    else {
        $raw = & $Path @Arguments 2>&1
    }
    $code = $LASTEXITCODE
    if ($null -ne $before) {
        try {
            [Console]::OutputEncoding = $before
        }
        catch {
            $before = $null
        }
    }
    $lines = @()
    foreach ($item in @($raw)) {
        if ($null -ne $item) {
            $lines += [string]$item
        }
    }
    return [pscustomobject]@{
        ExitCode = $code
        Lines    = $lines
    }
}

function Show-ToolText {
    # Prints what a program said, indented, so its own words stay visible.
    param($Result)
    foreach ($line in @($Result.Lines)) {
        $text = ([string]$line).TrimEnd()
        if ($text -ne '') {
            Write-Host ('         ' + $text) -ForegroundColor DarkGray
        }
    }
}

function Find-Usbipd {
    # The full path of usbipd.exe, or an empty string.
    $found = Get-Command -Name 'usbipd.exe' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $found) {
        return $found.Path
    }
    # Installed a moment ago? A window that was open before the install does not have it on its PATH yet.
    # The installer writes its folder to the registry (the 64-bit view, also from a 32-bit PowerShell).
    try {
        $machine = [Microsoft.Win32.RegistryHive]::LocalMachine
        $view64 = [Microsoft.Win32.RegistryView]::Registry64
        $hive = [Microsoft.Win32.RegistryKey]::OpenBaseKey($machine, $view64)
        $key = $hive.OpenSubKey('SOFTWARE\usbipd-win')
        if ($null -ne $key) {
            $folder = [string]$key.GetValue('APPLICATIONFOLDER')
            if (-not [string]::IsNullOrEmpty($folder)) {
                $candidate = Join-Path $folder 'usbipd.exe'
                if (Test-Path -LiteralPath $candidate) {
                    return $candidate
                }
            }
        }
    }
    catch {
        # The registry could not be read: treat usbipd as not installed.
        return ''
    }
    return ''
}

function Get-UsbipdVersion {
    # For example '5.3.0', or an empty string when it could not be read.
    param([string]$Usbipd)
    $result = Invoke-Tool -Path $Usbipd -Arguments @('--version')
    foreach ($line in @($result.Lines)) {
        $match = [regex]::Match([string]$line, '\d+\.\d+\.\d+')
        if ($match.Success) {
            return $match.Value
        }
    }
    return ''
}

function Find-Wsl {
    # The full path of wsl.exe, or an empty string.
    $found = Get-Command -Name 'wsl.exe' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $found) {
        return $found.Path
    }
    $windows = [Environment]::GetFolderPath('Windows')
    foreach ($folder in @('System32', 'Sysnative')) {
        $candidate = Join-Path (Join-Path $windows $folder) 'wsl.exe'
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    return ''
}

function Get-RunningDistributions {
    # The names of the WSL distributions that are running now (none when WSL is not installed, or when
    # wsl.exe answers with an error).
    param([string]$Wsl)
    $result = Invoke-Tool -Path $Wsl -Arguments @('--list', '--running', '--quiet')
    $names = @()
    if ($result.ExitCode -eq 0) {
        $nul = [string][char]0
        foreach ($line in @($result.Lines)) {
            # wsl.exe prints its own text as UTF-16 (unless WSL_UTF8=1 is set), which arrives here with a
            # NUL character after every letter. Distribution names are plain letters, digits, '.', '_'
            # and '-', so dropping the NULs gives the name either way.
            $name = ([string]$line).Replace($nul, '').Trim()
            if ($name -match '^[A-Za-z0-9._-]+$') {
                $names += $name
            }
        }
    }
    return $names
}

function Test-Elevated {
    # True when this PowerShell window was opened with "Run as administrator".
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object -TypeName Security.Principal.WindowsPrincipal -ArgumentList $identity
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-UsbDevices {
    # Every USB device usbipd knows, from "usbipd state" (JSON; the field names are usbipd's own:
    # Devices, BusId, ClientIPAddress, Description, InstanceId, IsForced, PersistedGuid).
    param([string]$Usbipd)
    $result = Invoke-Tool -Path $Usbipd -Arguments @('state') -DropErrorText
    if ($result.ExitCode -ne 0) {
        throw ('usbipd could not list the USB devices (exit code {0}).' -f $result.ExitCode)
    }
    $text = (@($result.Lines) -join "`n")
    if ($text.Trim() -eq '') {
        throw 'usbipd printed nothing when asked for the USB devices.'
    }
    try {
        $state = ConvertFrom-Json -InputObject $text
    }
    catch {
        throw 'usbipd answered, but its list of USB devices could not be read (not the JSON this script expects).'
    }
    $devices = @()
    foreach ($entry in @(Get-Prop $state 'Devices')) {
        if ($null -eq $entry) {
            continue
        }
        $instance = [string](Get-Prop $entry 'InstanceId')
        $vidPid = ''
        $match = [regex]::Match($instance, 'VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})')
        if ($match.Success) {
            $vidPid = ($match.Groups[1].Value + ':' + $match.Groups[2].Value).ToLowerInvariant()
        }
        $bus = [string](Get-Prop $entry 'BusId')
        $client = [string](Get-Prop $entry 'ClientIPAddress')
        $guid = [string](Get-Prop $entry 'PersistedGuid')
        $name = ''
        if ($vidPid -ne '' -and $KnownReceivers.ContainsKey($vidPid)) {
            $name = [string]$KnownReceivers[$vidPid]
        }
        $devices += [pscustomobject]@{
            BusId       = $bus
            Connected   = ($bus -ne '')
            Usable      = ($bus -match '^[1-9][0-9]?-[1-9][0-9]?$')
            VidPid      = $vidPid
            Name        = $name
            Description = [string](Get-Prop $entry 'Description')
            Shared      = ($guid -ne '')
            Attached    = ($client -ne '')
            Forced      = ((Get-Prop $entry 'IsForced') -eq $true)
        }
    }
    return $devices
}

function Get-DeviceLabel {
    param($Device)
    if ($Device.Name -ne '') {
        return ('{0}   (Windows calls it: {1})' -f $Device.Name, $Device.Description)
    }
    return $Device.Description
}

function Write-DeviceTable {
    param($Devices)
    Write-Host ('  {0,-6} {1,-10} {2,-11} {3}' -f 'BUSID', 'USB ID', 'STATE', 'DEVICE')
    foreach ($device in @($Devices)) {
        $state = 'not shared'
        if ($device.Attached) {
            $state = 'attached'
        }
        elseif ($device.Shared) {
            $state = 'shared'
        }
        $bus = $device.BusId
        if (-not $device.Usable) {
            $bus = '-'
        }
        Write-Host ('  {0,-6} {1,-10} {2,-11} {3}' -f $bus, $device.VidPid, $state, (Get-DeviceLabel $device))
    }
}

function Select-Receivers {
    # The connected devices this run handles: the receivers in the list and the bus ids the user named.
    param($Devices, [string[]]$Named)
    $chosen = @()
    foreach ($device in @($Devices)) {
        if (-not $device.Connected) {
            continue
        }
        if ($device.Name -ne '' -or (@($Named) -contains $device.BusId)) {
            $chosen += $device
        }
    }
    return $chosen
}

function Write-AttachHints {
    # Plain words for the usual reasons usbipd gives when an attach fails. The patterns are pieces of
    # usbipd's own messages.
    param($Result)
    $text = (@($Result.Lines) -join ' ')
    if ($text -match 'not USBIP capable' -or $text -match 'Loading vhci_hcd failed') {
        Write-More 'The Linux kernel of WSL is too old for USB devices. In PowerShell:  wsl --update'
        Write-More 'then:  wsl --shutdown   then open Ubuntu again and run this again.'
    }
    if ($text -match 'firewall appears to be blocking') {
        Write-More 'A firewall blocks the connection from WSL to usbipd on this computer (TCP port 3240).'
        Write-More 'usbipd-win adds its own rule to Windows Firewall; another firewall product needs one by hand.'
    }
    if ($text -match 'Device busy' -or $text -match 'used by Windows') {
        Write-More 'A Windows program is using the receiver. Close it (any SDR program) and run this again.'
        Write-More 'If no program has it, see "Device busy" in docs/WINDOWS.md.'
    }
    if ($text -match 'no WSL 2 distribution running' -or $text -match 'distribution is not running') {
        Write-More 'WSL is not running: open Ubuntu from the Start menu, leave its window open, run this again.'
    }
    if ($text -match 'WSL 1' -or $text -match 'only works with WSL 2') {
        Write-More 'The Linux distribution is set to WSL 1. USB devices need WSL 2:  wsl --set-version Ubuntu 2'
    }
    if ($text -match 'not shared') {
        Write-More 'The receiver is not shared yet: run this once in a PowerShell window opened as administrator.'
    }
    if ($text -match 'service is currently not running') {
        Write-More 'The usbipd service is not running. usbipd says a restart of Windows should fix that.'
    }
}

# ---------------------------------------------------------------------------------------------------------
# What was asked
# ---------------------------------------------------------------------------------------------------------

$modes = 0
if ($Attach) { $modes = $modes + 1 }
if ($Detach) { $modes = $modes + 1 }
if ($Auto) { $modes = $modes + 1 }
if ($modes -gt 1) {
    Write-Host 'Use only one of -Attach, -Detach and -Auto.'
    exit 2
}
if ($Unshare -and -not $Detach) {
    Write-Host '-Unshare only goes together with -Detach.'
    exit 2
}
$mode = 'list'
if ($Attach) { $mode = 'attach' }
if ($Detach) { $mode = 'detach' }
if ($Auto) { $mode = 'auto' }

$named = @()
foreach ($entry in @($BusId)) {
    if ($null -eq $entry) {
        continue
    }
    foreach ($piece in ([string]$entry).Split(',')) {
        $one = $piece.Trim()
        if ($one -eq '') {
            continue
        }
        if ($one -notmatch '^[1-9][0-9]?-[1-9][0-9]?$') {
            Write-Host ("-BusId '{0}' is not a bus id. A bus id looks like 2-3: take it from the list this script prints." -f $one)
            exit 2
        }
        if ($named -notcontains $one) {
            $named += $one
        }
    }
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    Write-Host 'Run this on Windows, in PowerShell. It does not belong inside WSL or on Linux.'
    exit 1
}

if ($mode -eq 'list') {
    Write-Host 'ARIS receivers for WSL: dry run. This lists what is there and changes NOTHING.'
}
elseif ($mode -eq 'attach') {
    Write-Host 'ARIS receivers for WSL: attaching.'
}
elseif ($mode -eq 'auto') {
    Write-Host 'ARIS receivers for WSL: attaching, and attaching again after a replug.'
}
else {
    Write-Host 'ARIS receivers for WSL: giving the receivers back to Windows.'
}

# ---------------------------------------------------------------------------------------------------------
# 1. Checks
# ---------------------------------------------------------------------------------------------------------

Write-Host ''
Write-Host '1. Checks'

$usbipd = Find-Usbipd
if ($usbipd -eq '') {
    Write-Fix 'usbipd-win is not installed. In PowerShell:  winget install --interactive --exact dorssel.usbipd-win'
    Write-More 'Save your work first: its installer may have to restart Windows.'
    Write-More 'Then open a NEW PowerShell window (the old one does not see the new program) and run this again.'
    Write-Host ''
    Write-Host 'Nothing was changed.'
    exit 1
}
$version = Get-UsbipdVersion -Usbipd $usbipd
if ($version -eq '') {
    Write-Note ('usbipd-win is installed ({0}); its version could not be read' -f $usbipd)
}
elseif ([int]($version.Split('.')[0]) -lt 4) {
    Write-Fix ('usbipd-win {0} is too old (4.0.0 or newer is needed). In PowerShell:  winget upgrade usbipd' -f $version)
    Write-Host ''
    Write-Host 'Nothing was changed.'
    exit 1
}
else {
    Write-Ok ('usbipd-win {0}' -f $version)
}

$devices = @()
try {
    $devices = @(Get-UsbDevices -Usbipd $usbipd)
}
catch {
    Write-Fix $_.Exception.Message
    Write-More 'Run "usbipd list" to see what usbipd says. A repair or re-install of usbipd-win usually fixes it.'
    Write-Host ''
    Write-Host 'Nothing was changed.'
    exit 1
}
Write-Ok ('usbipd lists {0} USB devices' -f @($devices | Where-Object { $_.Connected }).Count)

$wslRunning = $false
if ($mode -ne 'detach') {
    $wsl = Find-Wsl
    if ($wsl -eq '') {
        Write-Fix 'WSL is not installed. In a PowerShell window opened as administrator:  wsl --install'
    }
    else {
        $running = @(Get-RunningDistributions -Wsl $wsl)
        if ($running.Count -eq 0) {
            Write-Fix 'No WSL distribution is running for this Windows account.'
            Write-More 'Open Ubuntu from the Start menu and leave its window open. No Ubuntu yet? Install it first:'
            Write-More 'wsl --install   (docs/WINDOWS.md has the steps).'
            Write-More 'In an administrator window that belongs to another account, WSL is that account''s, not yours:'
            Write-More 'share the receivers there, then attach them from your own PowerShell window.'
        }
        else {
            $wslRunning = $true
            Write-Ok ('WSL is running: {0}' -f ($running -join ', '))
        }
    }
}

$elevated = Test-Elevated
if ($elevated) {
    Write-Ok 'this is an administrator window (needed the first time a receiver is shared)'
}
else {
    Write-Note 'this is not an administrator window (one is needed only the first time a receiver is shared)'
}

# ---------------------------------------------------------------------------------------------------------
# 2. Receivers found
# ---------------------------------------------------------------------------------------------------------

Write-Host ''
Write-Host '2. Receivers found'

foreach ($one in $named) {
    if (@($devices | Where-Object { $_.Connected -and $_.BusId -eq $one }).Count -eq 0) {
        Write-Fix ('-BusId {0}: no USB device is plugged in at that bus id (see the lists below)' -f $one)
    }
}

$selected = @(Select-Receivers -Devices $devices -Named $named)
if ($selected.Count -eq 0) {
    Write-Note 'no receiver this script knows is plugged in'
}
else {
    Write-DeviceTable -Devices $selected
}

$others = @($devices | Where-Object { $_.Connected -and $_.Name -eq '' -and ($named -notcontains $_.BusId) })
if ($mode -eq 'list' -and $others.Count -gt 0) {
    Write-Host ''
    Write-Host '   Other USB devices (left alone; to include one, add  -BusId <its bus id>):'
    Write-DeviceTable -Devices $others
}

if ($selected.Count -eq 0) {
    Write-Host ''
    Write-Host 'Nothing to do. Plug a receiver in, or name its bus id with -BusId. Nothing was changed.'
    if ($script:Problems -gt 0) { exit 1 }
    exit 0
}

# ---------------------------------------------------------------------------------------------------------
# Dry run: say what -Attach would do, and stop
# ---------------------------------------------------------------------------------------------------------

if ($mode -eq 'list') {
    Write-Host ''
    Write-Host '3. What -Attach would do'
    $needAdmin = $false
    foreach ($device in $selected) {
        if (-not $device.Usable) {
            Write-Fix ('{0}: is on a USB hub usbipd cannot use. Plug it into another USB port.' -f (Get-DeviceLabel $device))
        }
        elseif ($device.Attached) {
            Write-Ok ('{0}: already attached, nothing to do' -f $device.BusId)
        }
        elseif ($device.Shared) {
            Write-Host ('       {0}: attach it to WSL' -f $device.BusId)
        }
        else {
            $needAdmin = $true
            Write-Host ('       {0}: share it (once), then attach it to WSL' -f $device.BusId)
        }
    }
    if ($needAdmin -and -not $elevated) {
        Write-Note 'sharing needs an administrator: open PowerShell with "Run as administrator" for the first -Attach'
    }
    Write-Host ''
    Write-Host 'Dry run: nothing was changed. To do it, run this again with -Attach (or -Auto to keep attaching'
    Write-Host 'after a replug). While a receiver is attached to WSL, Windows programs cannot use it; -Detach gives it back.'
    if ($script:Problems -gt 0) { exit 1 }
    exit 0
}

# ---------------------------------------------------------------------------------------------------------
# -Detach
# ---------------------------------------------------------------------------------------------------------

if ($mode -eq 'detach') {
    Write-Host ''
    Write-Host '3. Giving the receivers back to Windows'
    foreach ($device in $selected) {
        if (-not $device.Attached) {
            Write-Ok ('{0}: was not attached' -f $device.BusId)
            continue
        }
        $result = Invoke-Tool -Path $usbipd -Arguments @('detach', '--busid', $device.BusId)
        Show-ToolText $result
        if ($result.ExitCode -eq 0 -and $device.Forced) {
            Write-Ok ('{0}: detached' -f $device.BusId)
            Write-More 'It was shared with "usbipd bind --force", so Windows gets it back only after -Detach -Unshare.'
        }
        elseif ($result.ExitCode -eq 0) {
            Write-Ok ('{0}: detached, Windows has it again' -f $device.BusId)
        }
        else {
            Write-Fix ('{0}: usbipd could not detach it (exit code {1}); its message is above' -f $device.BusId, $result.ExitCode)
        }
    }
    if ($Unshare) {
        Write-Host ''
        Write-Host '4. Stop sharing them'
        foreach ($device in $selected) {
            if (-not $device.Shared) {
                Write-Ok ('{0}: was not shared' -f $device.BusId)
                continue
            }
            if (-not $device.Usable) {
                continue
            }
            if (-not $elevated) {
                Write-Fix ('{0}: to stop sharing, run this in a PowerShell window opened as administrator' -f $device.BusId)
                continue
            }
            $result = Invoke-Tool -Path $usbipd -Arguments @('unbind', '--busid', $device.BusId)
            Show-ToolText $result
            if ($result.ExitCode -eq 0) {
                Write-Ok ('{0}: no longer shared' -f $device.BusId)
            }
            else {
                Write-Fix ('{0}: usbipd could not stop sharing it (exit code {1}); its message is above' -f $device.BusId, $result.ExitCode)
            }
        }
    }
    else {
        Write-Host ''
        Write-Host 'The receivers stay shared, so the next -Attach needs no administrator. To stop sharing them as'
        Write-Host 'well: -Detach -Unshare in an administrator window.'
    }
    if ($script:Problems -gt 0) { exit 1 }
    exit 0
}

# ---------------------------------------------------------------------------------------------------------
# -Attach and -Auto: 3. share
# ---------------------------------------------------------------------------------------------------------

Write-Host ''
Write-Host '3. Sharing the receivers (once per receiver; Windows remembers it)'
foreach ($device in $selected) {
    if (-not $device.Usable) {
        Write-Fix ('{0}: is on a USB hub usbipd cannot use. Plug it into another USB port.' -f (Get-DeviceLabel $device))
        continue
    }
    if ($device.Shared) {
        Write-Ok ('{0}: already shared' -f $device.BusId)
        continue
    }
    if (-not $elevated) {
        Write-Fix ('{0}: not shared yet, and sharing needs an administrator' -f $device.BusId)
        Write-More 'Open PowerShell with "Run as administrator" and run this same command once. After that an'
        Write-More 'ordinary window is enough.'
        continue
    }
    $result = Invoke-Tool -Path $usbipd -Arguments @('bind', '--busid', $device.BusId)
    Show-ToolText $result
    if ($result.ExitCode -eq 0) {
        Write-Ok ('{0}: shared' -f $device.BusId)
    }
    elseif ($result.ExitCode -eq 3) {
        Write-Fix ('{0}: usbipd refused: it needs an administrator window' -f $device.BusId)
    }
    else {
        Write-Fix ('{0}: usbipd could not share it (exit code {1}); its message is above' -f $device.BusId, $result.ExitCode)
    }
}

# Read the state again: it now says which receivers are shared.
try {
    $devices = @(Get-UsbDevices -Usbipd $usbipd)
}
catch {
    Write-Fix $_.Exception.Message
    exit 1
}
$selected = @(Select-Receivers -Devices $devices -Named $named)
$ready = @($selected | Where-Object { $_.Usable -and $_.Shared })

Write-Host ''
if ($ready.Count -eq 0) {
    Write-Host 'No receiver is shared, so none was attached. See the lines marked FIX.'
    exit 1
}
if (-not $wslRunning) {
    Write-Host '4. Attaching to WSL'
    Write-Fix 'skipped: WSL is not running for this Windows account (see 1.)'
    Write-More 'What is shared stays shared. Open Ubuntu, then run this again from an ordinary PowerShell window.'
    exit 1
}

# ---------------------------------------------------------------------------------------------------------
# -Auto: 4. one "usbipd attach --auto-attach" per receiver, in this window, until Ctrl+C
# ---------------------------------------------------------------------------------------------------------

if ($mode -eq 'auto') {
    Write-Host '4. Attaching, and attaching again after a replug'
    Write-Host '   usbipd runs one endless loop per receiver. Their messages appear below.'
    Write-Host '   Leave this window open. Press Ctrl+C to stop. The loops also end when WSL is shut down.'
    Write-Host '   A receiver moved to another USB port gets another bus id: stop this and start it again.'
    Write-Host ''
    $loops = @()
    try {
        foreach ($device in $ready) {
            $arguments = @('attach', '--wsl', '--busid', $device.BusId, '--auto-attach')
            $process = Start-Process -FilePath $usbipd -ArgumentList $arguments -NoNewWindow -PassThru
            # Reading the handle now keeps it, so the exit code can still be read after the program ended.
            $null = $process.Handle
            $loops += [pscustomobject]@{
                Process = $process
                BusId   = $device.BusId
            }
            Write-Ok ('{0}: loop started' -f $device.BusId)
        }
        while (@($loops | Where-Object { -not $_.Process.HasExited }).Count -gt 0) {
            Start-Sleep -Seconds 2
        }
    }
    finally {
        # Ctrl+C reaches usbipd as well, and usbipd then stops its helper inside WSL. Give it a moment
        # to do that before ending whatever is left.
        foreach ($loop in @($loops)) {
            if (-not $loop.Process.HasExited) {
                $null = $loop.Process.WaitForExit(5000)
            }
            if (-not $loop.Process.HasExited) {
                try {
                    $loop.Process.Kill()
                }
                catch {
                    Write-Host ('   could not end the loop for {0}' -f $loop.BusId)
                }
            }
        }
    }
    Write-Host ''
    Write-Note 'every loop has ended: WSL was shut down, or usbipd reported a problem above'
    Write-More 'Open Ubuntu again and run this again to attach the receivers.'
    exit 1
}

# ---------------------------------------------------------------------------------------------------------
# -Attach: 4. attach
# ---------------------------------------------------------------------------------------------------------

Write-Host '4. Attaching to WSL'
foreach ($device in $ready) {
    if ($device.Attached) {
        Write-Ok ('{0}: already attached' -f $device.BusId)
        continue
    }
    $result = Invoke-Tool -Path $usbipd -Arguments @('attach', '--wsl', '--busid', $device.BusId)
    Show-ToolText $result
    if ($result.ExitCode -eq 0) {
        Write-Ok ('{0}: attached to WSL   ({1})' -f $device.BusId, (Get-DeviceLabel $device))
    }
    else {
        Write-Fix ('{0}: usbipd could not attach it (exit code {1}); its message is above' -f $device.BusId, $result.ExitCode)
        Write-AttachHints $result
    }
}

# Show how things stand now, as usbipd reports it.
try {
    $devices = @(Get-UsbDevices -Usbipd $usbipd)
    $selected = @(Select-Receivers -Devices $devices -Named $named)
    Write-Host ''
    Write-Host '   Now:'
    Write-DeviceTable -Devices $selected
}
catch {
    Write-Note 'the list could not be read again; "usbipd list" shows how things stand'
}

Write-Host ''
Write-Host '5. Next'
Write-Host '   In Ubuntu:        lsusb                    the receiver should be in the list'
Write-Host '   In any browser:   http://localhost:5000    then type /check in the chat'
Write-Host '   Attach again after the receiver was unplugged, after Windows restarted and after "wsl --shutdown"'
Write-Host '   (or use -Auto, which attaches again after a replug). -Detach gives the receivers back to Windows.'
if ($script:Problems -gt 0) { exit 1 }
exit 0
