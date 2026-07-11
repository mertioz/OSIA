# Analyser l'espace disque
Write-Host "=== TAILLE DES DOSSIERS PRINCIPAUX (Go) ==="
Get-ChildItem C:\ -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $size = (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
    $sizeGB = [math]::Round($size / 1GB, 2)
    if ($sizeGB -gt 0) {
        [PSCustomObject]@{Dossier=$_.FullName; Taille_Go=$sizeGB}
    }
} | Sort-Object Taille_Go -Descending | Format-Table -AutoSize

Write-Host "`n=== TOP 100 FICHIERS LES PLUS LOURDS ==="
Get-ChildItem C:\ -Recurse -File -ErrorAction SilentlyContinue |
    Sort-Object Length -Descending |
    Select-Object -First 100 |
    ForEach-Object {
        [PSCustomObject]@{Chemin=$_.FullName; Taille_Mo=[math]::Round($_.Length / 1MB, 2); Taille_Go=[math]::Round($_.Length / 1GB, 2)}
    } | Format-Table -AutoSize

Write-Host "`n=== RÉPARTITION PAR TYPE DE FICHIER (extensions) ==="
Get-ChildItem C:\ -Recurse -File -ErrorAction SilentlyContinue |
    Group-Object Extension |
    Sort-Object Count -Descending |
    Select-Object -First 20 |
    ForEach-Object {
        $totalSize = ($_.Group | Measure-Object -Property Length -Sum).Sum
        [PSCustomObject]@{Extension=$_.Name; Nombre=$_.Count; Taille_Go=[math]::Round($totalSize / 1GB, 2)}
    } | Format-Table -AutoSize

Write-Host "`n=== ANALYSE DU PROFIL UTILISATEUR ==="
$userDirs = @('AppData', 'Documents', 'Downloads', 'Desktop', 'Music', 'Pictures', 'Videos')
foreach ($dir in $userDirs) {
    $path = Join-Path $env:USERPROFILE $dir
    if (Test-Path $path) {
        $size = (Get-ChildItem $path -Recurse -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
        $sizeGB = [math]::Round($size / 1GB, 2)
        Write-Host "$dir : $sizeGB Go"
    }
}
