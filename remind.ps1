# Option B reminder: pops a dialog reminding you to run the Mandarin workflow,
# and (on Yes) opens a ready-to-go PowerShell window in the project with the
# venv activated. Triggered by the "MandarinClassRecap" scheduled task.
# Run manage_class.py yourself there so you get the interactive review loop.

Add-Type -AssemblyName System.Windows.Forms

$projectDir = "e:\code_collection\mandarin-workflow"

$result = [System.Windows.Forms.MessageBox]::Show(
    "Time to prepare this week's Mandarin class!`n`nGenerate the next lesson plan (with review) and the recap draft?`n`nClick Yes to open the project terminal.",
    "Mandarin Class Recap",
    [System.Windows.Forms.MessageBoxButtons]::YesNo,
    [System.Windows.Forms.MessageBoxIcon]::Information
)

if ($result -eq [System.Windows.Forms.DialogResult]::Yes) {
    # Open a new PowerShell window, activate the venv, and print the run command.
    $inner = @"
Set-Location '$projectDir'
& '.\.venv\Scripts\Activate.ps1'
Write-Host ''
Write-Host 'Ready. Review the lesson plan when prompted, then it will draft the recap.' -ForegroundColor Cyan
Write-Host 'Run:  python manage_class.py' -ForegroundColor Yellow
Write-Host ''
"@
    Start-Process powershell.exe -ArgumentList @(
        "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $inner
    )
}
