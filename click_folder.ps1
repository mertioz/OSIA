$shell = New-Object -ComObject Shell.Application
$desktop = $shell.Namespace(0)
$items = $desktop.Items()
foreach ($item in $items) {
    if ($item.Name -eq 'ChargesFreelance') {
        Write-Host ('Found: ' + $item.Name)
        $item.InvokeVerb('open')
    }
}
