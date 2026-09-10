
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ErrorActionPreference = "Stop"

$InstallDir = "C:\Program Files\Android_Explorer"
$PythonVersion = "3.13.9"
$PythonTag = "313"
$PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$ScrcpyVersion = "4.1"
$ScrcpyZip = "scrcpy-win64-v$ScrcpyVersion.zip"
$ScrcpyUrl = "https://github.com/Genymobile/scrcpy/releases/download/v$ScrcpyVersion/$ScrcpyZip"
$ScrcpySha256 = "5b12172b3264b2889f4583ee64752ce832e29bc8b1089dca81093459697165db"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    $args = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    Start-Process powershell.exe -Verb RunAs -ArgumentList $args
    exit
}

function Set-Status([string]$text, [int]$value) {
    $script:statusLabel.Text = $text
    $script:progress.Value = [Math]::Max(0, [Math]::Min(100, $value))
    [System.Windows.Forms.Application]::DoEvents()
}

function Download-File([string]$url, [string]$dest) {
    $wc = New-Object System.Net.WebClient
    $wc.DownloadFile($url, $dest)
}

function Create-Shortcut([string]$path) {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($path)
    $sc.TargetPath = Join-Path $InstallDir "python3\pythonw.exe"
    $sc.Arguments = '"' + (Join-Path $InstallDir "android_explorer.py") + '"'
    $sc.WorkingDirectory = $InstallDir
    $sc.IconLocation = (Join-Path $InstallDir "android_explorer.ico") + ",0"
    $sc.Description = "Android Explorer"
    $sc.Save()
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "Android Explorer Setup"
$form.StartPosition = "CenterScreen"
$form.Size = New-Object System.Drawing.Size(520, 330)
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $true
$form.BackColor = [System.Drawing.Color]::FromArgb(245,245,245)

$iconPath = Join-Path $ScriptDir "android_explorer.ico"
if (Test-Path $iconPath) {
    try { $form.Icon = New-Object System.Drawing.Icon($iconPath) } catch {}
}

$title = New-Object System.Windows.Forms.Label
$title.Text = "Install Android Explorer"
$title.Font = New-Object System.Drawing.Font("Segoe UI", 18, [System.Drawing.FontStyle]::Bold)
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(24, 22)
$form.Controls.Add($title)

$desc = New-Object System.Windows.Forms.Label
$desc.Text = "Installs Android Explorer and its portable dependencies to:`r`n$InstallDir"
$desc.Font = New-Object System.Drawing.Font("Segoe UI", 10)
$desc.AutoSize = $true
$desc.Location = New-Object System.Drawing.Point(27, 67)
$form.Controls.Add($desc)

$desktopCheck = New-Object System.Windows.Forms.CheckBox
$desktopCheck.Text = "Create Desktop shortcut"
$desktopCheck.Checked = $true
$desktopCheck.AutoSize = $true
$desktopCheck.Location = New-Object System.Drawing.Point(30, 120)
$form.Controls.Add($desktopCheck)

$startCheck = New-Object System.Windows.Forms.CheckBox
$startCheck.Text = "Create Start Menu shortcut"
$startCheck.Checked = $true
$startCheck.AutoSize = $true
$startCheck.Location = New-Object System.Drawing.Point(30, 148)
$form.Controls.Add($startCheck)

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Text = "Ready to install."
$statusLabel.AutoSize = $true
$statusLabel.Location = New-Object System.Drawing.Point(30, 190)
$form.Controls.Add($statusLabel)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Location = New-Object System.Drawing.Point(30, 214)
$progress.Size = New-Object System.Drawing.Size(450, 22)
$progress.Minimum = 0
$progress.Maximum = 100
$form.Controls.Add($progress)

$installButton = New-Object System.Windows.Forms.Button
$installButton.Text = "Install"
$installButton.Size = New-Object System.Drawing.Size(100, 32)
$installButton.Location = New-Object System.Drawing.Point(274, 252)
$form.Controls.Add($installButton)

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Text = "Cancel"
$cancelButton.Size = New-Object System.Drawing.Size(100, 32)
$cancelButton.Location = New-Object System.Drawing.Point(382, 252)
$cancelButton.Add_Click({ $form.Close() })
$form.Controls.Add($cancelButton)

$installButton.Add_Click({
    try {
        $installButton.Enabled = $false
        $cancelButton.Enabled = $false
        $desktopCheck.Enabled = $false
        $startCheck.Enabled = $false

        $tmp = Join-Path $env:TEMP ("AndroidExplorerSetup_" + [Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tmp -Force | Out-Null

        Set-Status "Creating install directory..." 5
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

        Set-Status "Copying Android Explorer..." 10
        Copy-Item (Join-Path $ScriptDir "android_explorer.py") (Join-Path $InstallDir "android_explorer.py") -Force
        Copy-Item (Join-Path $ScriptDir "android_explorer.ico") (Join-Path $InstallDir "android_explorer.ico") -Force
        Copy-Item (Join-Path $ScriptDir "android_explorer.png") (Join-Path $InstallDir "android_explorer.png") -Force

        Set-Status "Downloading portable Python..." 20
        $pyZip = Join-Path $tmp "python.zip"
        Download-File $PythonUrl $pyZip

        $pyDir = Join-Path $InstallDir "python3"
        if (Test-Path $pyDir) { Remove-Item $pyDir -Recurse -Force }
        New-Item -ItemType Directory -Path $pyDir -Force | Out-Null

        Set-Status "Extracting Python..." 30
        Expand-Archive -LiteralPath $pyZip -DestinationPath $pyDir -Force

        $pth = Join-Path $pyDir "python$PythonTag._pth"
        $lines = Get-Content -LiteralPath $pth
        $lines = $lines -replace '^#import site$', 'import site'
        if (-not ($lines -contains 'Lib\site-packages')) {
            $lines += 'Lib\site-packages'
        }
        Set-Content -LiteralPath $pth -Value $lines -Encoding ASCII
        New-Item -ItemType Directory -Path (Join-Path $pyDir "Lib\site-packages") -Force | Out-Null

        Set-Status "Installing Python dependencies..." 42
        $getPip = Join-Path $tmp "get-pip.py"
        Download-File "https://bootstrap.pypa.io/get-pip.py" $getPip
        & (Join-Path $pyDir "python.exe") $getPip --no-warn-script-location --disable-pip-version-check
        if ($LASTEXITCODE -ne 0) { throw "pip bootstrap failed." }

        & (Join-Path $pyDir "python.exe") -m pip install --upgrade --only-binary=:all: --no-warn-script-location --disable-pip-version-check PySide6-Essentials
        if ($LASTEXITCODE -ne 0) { throw "PySide6-Essentials installation failed." }

        Set-Status "Downloading scrcpy..." 62
        $scrcpyZip = Join-Path $tmp $ScrcpyZip
        Download-File $ScrcpyUrl $scrcpyZip

        $actualHash = (Get-FileHash -LiteralPath $scrcpyZip -Algorithm SHA256).Hash.ToLower()
        if ($actualHash -ne $ScrcpySha256) {
            throw "scrcpy checksum verification failed."
        }

        $extractDir = Join-Path $tmp "scrcpy_extract"
        Expand-Archive -LiteralPath $scrcpyZip -DestinationPath $extractDir -Force
        $srcScrcpy = Join-Path $extractDir "scrcpy-win64-v$ScrcpyVersion"
        if (-not (Test-Path (Join-Path $srcScrcpy "scrcpy.exe"))) {
            throw "scrcpy.exe was not found after extraction."
        }

        $scrcpyDir = Join-Path $InstallDir "scrcpy"
        if (Test-Path $scrcpyDir) { Remove-Item $scrcpyDir -Recurse -Force }
        New-Item -ItemType Directory -Path $scrcpyDir -Force | Out-Null
        Copy-Item (Join-Path $srcScrcpy "*") $scrcpyDir -Recurse -Force

        Set-Status "Creating launcher..." 78
        $launcher = @'
@echo off
setlocal
cd /d "%~dp0"
set "PATH=%~dp0scrcpy;%PATH%"
start "" "%~dp0python3\pythonw.exe" "%~dp0android_explorer.py"
'@
        Set-Content -LiteralPath (Join-Path $InstallDir "Android Explorer.bat") -Value $launcher -Encoding ASCII

        Set-Status "Creating shortcuts..." 86

        if ($desktopCheck.Checked) {
            $desktop = [Environment]::GetFolderPath("Desktop")
            Create-Shortcut (Join-Path $desktop "Android Explorer.lnk")
        }

        if ($startCheck.Checked) {
            $startMenu = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs"
            Create-Shortcut (Join-Path $startMenu "Android Explorer.lnk")
        }

        # Simple uninstaller
        $uninstallPs1 = @"
Add-Type -AssemblyName System.Windows.Forms
`$r = [System.Windows.Forms.MessageBox]::Show(
    'Remove Android Explorer from this computer?',
    'Uninstall Android Explorer',
    [System.Windows.Forms.MessageBoxButtons]::YesNo,
    [System.Windows.Forms.MessageBoxIcon]::Question
)
if (`$r -ne [System.Windows.Forms.DialogResult]::Yes) { exit }

`$desktop = [Environment]::GetFolderPath('Desktop')
Remove-Item (Join-Path `$desktop 'Android Explorer.lnk') -Force -ErrorAction SilentlyContinue
Remove-Item 'C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Android Explorer.lnk' -Force -ErrorAction SilentlyContinue
Start-Process cmd.exe -WindowStyle Hidden -ArgumentList '/c timeout /t 2 /nobreak >nul & rmdir /s /q "C:\Program Files\Android_Explorer"'
"@
        Set-Content -LiteralPath (Join-Path $InstallDir "Uninstall.ps1") -Value $uninstallPs1 -Encoding UTF8

        Set-Status "Verifying installation..." 94
        if (-not (Test-Path (Join-Path $pyDir "pythonw.exe"))) { throw "pythonw.exe is missing." }
        if (-not (Test-Path (Join-Path $scrcpyDir "scrcpy.exe"))) { throw "scrcpy.exe is missing." }
        if (-not (Test-Path (Join-Path $scrcpyDir "adb.exe"))) { throw "adb.exe is missing." }

        # Verify PySide6 can import.
        & (Join-Path $pyDir "python.exe") -c "from PySide6.QtCore import Qt; from PySide6.QtGui import QIcon; from PySide6.QtWidgets import QApplication,QMainWindow; print('OK')"
        if ($LASTEXITCODE -ne 0) { throw "PySide6 verification failed." }

        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

        Set-Status "Installation complete." 100
        [System.Windows.Forms.MessageBox]::Show(
            "Android Explorer was installed successfully.`r`n`r`n$InstallDir",
            "Android Explorer Setup",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information
        )

        $launch = [System.Windows.Forms.MessageBox]::Show(
            "Launch Android Explorer now?",
            "Android Explorer Setup",
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Question
        )
        if ($launch -eq [System.Windows.Forms.DialogResult]::Yes) {
            Start-Process (Join-Path $pyDir "pythonw.exe") -WorkingDirectory $InstallDir -ArgumentList ('"' + (Join-Path $InstallDir "android_explorer.py") + '"')
        }

        $form.Close()
    }
    catch {
        try { if ($tmp -and (Test-Path $tmp)) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue } } catch {}
        Set-Status "Installation failed." 0
        [System.Windows.Forms.MessageBox]::Show(
            $_.Exception.Message,
            "Android Explorer Setup Error",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        )
        $installButton.Enabled = $true
        $cancelButton.Enabled = $true
        $desktopCheck.Enabled = $true
        $startCheck.Enabled = $true
    }
})

[void]$form.ShowDialog()
