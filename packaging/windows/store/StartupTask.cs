using System;
using System.Threading.Tasks;
using Windows.ApplicationModel;

internal static class StoreStartup {
    private static async Task<int> Change(string id, string action) {
        if (Package.Current.Id.FamilyName != "GreatInvention.OSIA_ge7crsndb7e8c") return 2;
        var task = await StartupTask.GetAsync(id);
        if (action == "disable") task.Disable();
        var state = task.State;
        if (action == "enable" && state == StartupTaskState.Disabled)
            state = await task.RequestEnableAsync();
        bool enabled = state == StartupTaskState.Enabled || state == StartupTaskState.EnabledByPolicy;
        if (action == "enable" && state == StartupTaskState.DisabledByUser) return 3;
        return (action == "disable" ? !enabled : enabled) ? 0 : 1;
    }

    [STAThread]
    private static int Main(string[] args) {
        if (args.Length != 2 || (args[0] != "OsiaDesktopStartup" && args[0] != "OsiaServerStartup") ||
            (args[1] != "state" && args[1] != "enable" && args[1] != "disable")) return 2;
        try { return Change(args[0], args[1]).GetAwaiter().GetResult(); }
        catch (Exception) { return 2; }
    }
}
