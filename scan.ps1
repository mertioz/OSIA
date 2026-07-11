# Analyse ciblée des dossiers volumineux
Write-Host "=== Profil utilisateur (C:\Users\vertu) ==="
$profile = $env:USERPROFILE
$suspects = @('AppData', 'Documents', 'Downloads', 'Desktop', 'Music', 'Pictures', 'Videos', 'OneDrive', '.cache')
foreach ($dir in $suspects) {
    $path = Join-Path $profile $dir
    if (Test-Path $path) {
        $size = (Get-ChildItem $path -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
        $gb = [math]::Round($size / 1GB, 2)
        if ($gb -gt 0.01) { Write-Host "$dir : $gb Go" }
    }
}

Write-Host "`n=== Program Files (top 15) ==="
Get-ChildItem "C:\Program Files" -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $size = (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    [PSCustomObject]@{Name=$_.Name; SizeGB=[math]::Round($size/1GB,2)}
} | Sort-Object SizeGB -Descending | Select-Object -First 15 | Format-Table -AutoSize

Write-Host "`n=== Program Files (x86) (top 10) ==="
Get-ChildItem "C:\Program Files (x86)" -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $size = (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    [PSCustomObject]@{Name=$_.Name; SizeGB=[math]::Round($size/1GB,2)}
} | Sort-Object SizeGB -Descending | Select-Object -First 10 | Format-Table -AutoSize

Write-Host "`n=== Autres dossiers suspects ==="
$others = @('C:\Windows', 'C:\ProgramData', 'C:\XboxGames', 'C:\temp', 'C:\src', 'C:\MSI', 'C:\User Manual')
foreach ($d in $others) {
    if (Test-Path $d) {
        $size = (Get-ChildItem $d -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
        $gb = [math]::Round($size / 1GB, 2)
        Write-Host "$d : $gb Go"
    }
}
