Write-Host 'Scanning top-level folders...'
$topLevel = Get-ChildItem C:\ -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Attributes -notmatch 'ReparsePoint' }
$allItems = @()

# 1. Scan top-level directories using robocopy
foreach ($dir in $topLevel) {
    $path = $dir.FullName
    Write-Host "  Scanning $path ..."
    $bytes = $null
    try {
        $output = & robocopy "$path" NULL /l /e /njh /njs /nc /np /nfl /ndl /bytes 2>&1 | Out-String
        if ($output -match 'Bytes\s*:\s*([\d\.,]+)\s*([a-zA-Z]*)') {
            $sizeNum = [double]($matches[1] -replace '[^\d\.]','')
            $unit = $matches[2].ToLower()
            switch ($unit) {
                'k' { $bytes = $sizeNum * 1KB }
                'm' { $bytes = $sizeNum * 1MB }
                'g' { $bytes = $sizeNum * 1GB }
                't' { $bytes = $sizeNum * 1TB }
                default { $bytes = $sizeNum } # bytes already
            }
        }
    } catch {}
    if ($bytes) {
        $allItems += [PSCustomObject]@{Type='Folder'; Path=$path; SizeBytes=$bytes}
    }
}

Write-Host 'Top-level done. Sorting...'
$allItems = $allItems | Sort-Object SizeBytes -Descending

# 2. For the 15 largest folders, drill down one level into their subdirectories
$drillList = $allItems | Select-Object -First 15
foreach ($parent in $drillList) {
    $subDirs = Get-ChildItem $parent.Path -Directory -ErrorAction SilentlyContinue
    foreach ($sub in $subDirs) {
        $path = $sub.FullName
        Write-Host "  Scanning subfolder $path ..."
        $bytes = $null
        try {
            $output = & robocopy "$path" NULL /l /e /njh /njs /nc /np /nfl /ndl /bytes 2>&1 | Out-String
            if ($output -match 'Bytes\s*:\s*([\d\.,]+)\s*([a-zA-Z]*)') {
                $sizeNum = [double]($matches[1] -replace '[^\d\.]','')
                $unit = $matches[2].ToLower()
                switch ($unit) {
                    'k' { $bytes = $sizeNum * 1KB }
                    'm' { $bytes = $sizeNum * 1MB }
                    'g' { $bytes = $sizeNum * 1GB }
                    't' { $bytes = $sizeNum * 1TB }
                    default { $bytes = $sizeNum }
                }
            }
        } catch {}
        if ($bytes) {
            $allItems += [PSCustomObject]@{Type='Folder'; Path=$path; SizeBytes=$bytes}
        }
    }
}

Write-Host 'Second level done. Drilling deeper...'
# 3. For the 5 largest of those subfolders, go one more level
$drillList2 = $allItems | Sort-Object SizeBytes -Descending | Select-Object -First 25
# pick those that were subfolders (i.e., depth > 1 in C:\)
$targets = @()
foreach ($item in $drillList2) {
    if ($item.Type -eq 'Folder' -and $item.Path -ne 'C:\') {
        $depth = ($item.Path -replace '^C:\\', '').Split('\').Count
        if ($depth -ge 2) {
            $targets += $item
        }
    }
}
$targets = $targets | Sort-Object SizeBytes -Descending | Select-Object -First 5
foreach ($parent in $targets) {
    $subDirs = Get-ChildItem $parent.Path -Directory -ErrorAction SilentlyContinue
    foreach ($sub in $subDirs) {
        $path = $sub.FullName
        Write-Host "  Scanning sub-subfolder $path ..."
        $bytes = $null
        try {
            $output = & robocopy "$path" NULL /l /e /njh /njs /nc /np /nfl /ndl /bytes 2>&1 | Out-String
            if ($output -match 'Bytes\s*:\s*([\d\.,]+)\s*([a-zA-Z]*)') {
                $sizeNum = [double]($matches[1] -replace '[^\d\.]','')
                $unit = $matches[2].ToLower()
                switch ($unit) {
                    'k' { $bytes = $sizeNum * 1KB }
                    'm' { $bytes = $sizeNum * 1MB }
                    'g' { $bytes = $sizeNum * 1GB }
                    't' { $bytes = $sizeNum * 1TB }
                    default { $bytes = $sizeNum }
                }
            }
        } catch {}
        if ($bytes) {
            $allItems += [PSCustomObject]@{Type='Folder'; Path=$path; SizeBytes=$bytes}
        }
    }
}

Write-Host 'Scan for large files (top 200 by size)...'
# 4. Large files using dir /o-s (sorted by size descending) – capture top 200. We'll parse output.
$largeFiles = @()
$output = cmd /c "dir C:\ /s /a-d /-c /o-s 2>nul" | Select-Object -First 400
# Parse dir output: lines like "2025-06-15  10:30 AM    12,345,678 file.txt"
foreach ($line in $output) {
    if ($line -match '^\s*\d{4}-\d{2}-\d{2}' -and $line -match '\s+(\d{1,3}(?:,\d{3})*)\s+') {
        $size = [decimal]($matches[1] -replace ',','')
        $filePath = $line.Substring($line.LastIndexOf(' ')).Trim()
        $fullPath = $filePath
        # reconstruct full path: the dir command shows full path? Actually dir /s shows full path in header then relative filenames. We'll need to rebuild.
        # Simpler: use previous output as directory header. We'll skip for now and rely on a different method.
    }
}
# Better to use PowerShell for file scan.
$largeFiles = Get-ChildItem C:\ -File -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.Length -gt 10MB } | Sort-Object Length -Descending | Select-Object -First 300
foreach ($f in $largeFiles) {
    $allItems += [PSCustomObject]@{Type='File'; Path=$f.FullName; SizeBytes=$f.Length}
}

Write-Host 'Combining and sorting all items...'
$final = $allItems | Sort-Object SizeBytes -Descending -Unique | Select-Object -First 100

Write-Host ''
Write-Host '========== TOP 100 LARGEST ITEMS ON C: =========='
$index = 1
foreach ($item in $final) {
    $sizeFormatted = if ($item.SizeBytes -ge 1TB) {
        "{0:N2} TB" -f ($item.SizeBytes / 1TB)
    } elseif ($item.SizeBytes -ge 1GB) {
        "{0:N2} GB" -f ($item.SizeBytes / 1GB)
    } elseif ($item.SizeBytes -ge 1MB) {
        "{0:N2} MB" -f ($item.SizeBytes / 1MB)
    } else {
        "{0:N0} KB" -f ($item.SizeBytes / 1KB)
    }
    Write-Host ("{0,3}. [{1}] {2}  -  {3}" -f $index, $item.Type, $sizeFormatted, $item.Path)
    $index++
}

$final | Export-Csv -Path "C:\Users\vertu\osia\largest_items.csv" -NoTypeInformation
Write-Host "`nSaved to C:\Users\vertu\osia\largest_items.csv"
