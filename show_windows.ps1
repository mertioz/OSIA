Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Diagnostics;
public class WinActivate {
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hWnd);
    
    public static void ActivateWindow(string procName) {
        Process[] procs = Process.GetProcessesByName(procName);
        foreach (Process p in procs) {
            if (p.MainWindowHandle != IntPtr.Zero) {
                if (IsIconic(p.MainWindowHandle)) {
                    ShowWindow(p.MainWindowHandle, 9); // SW_RESTORE
                }
                SetForegroundWindow(p.MainWindowHandle);
                Console.WriteLine("Activated: " + procName + " - " + p.MainWindowTitle);
                return;
            }
        }
        Console.WriteLine("No window found for: " + procName);
    }
}
"@

# Activer chaque app
$apps = @('Discord', 'Steam', 'XboxPcApp', 'XboxApp', 'Notepad', 'notepad++')
foreach ($app in $apps) {
    [WinActivate]::ActivateWindow($app)
}

Write-Host "Done"
