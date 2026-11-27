/* A movable Finder/file-manager entry point for the bundled Python runtime. */
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#import <ServiceManagement/ServiceManagement.h>

/* Run in the main .app executable so ServiceManagement identifies OSIA,
   not the embedded Python interpreter. Status 3 means user approval is due. */
static int login_item(const char *action) {
    @autoreleasepool {
        SMAppService *item = [SMAppService mainAppService];
        SMAppServiceStatus state = item.status;
        if (strcmp(action, "state") == 0) {
            if (state == SMAppServiceStatusRequiresApproval) return 3;
            return state == SMAppServiceStatusEnabled ? 0 : 1;
        }
        if (strcmp(action, "disable") == 0) {
            if (state == SMAppServiceStatusNotRegistered) return 0;
            return [item unregisterAndReturnError:NULL] ? 0 : 2;
        }
        int automatic = strcmp(action, "default") == 0;
        if (!automatic && strcmp(action, "enable") != 0) return 2;
        if (state == SMAppServiceStatusNotRegistered) {
            NSError *error = nil;
            if (![item registerAndReturnError:&error] && error != nil)
                fprintf(stderr, "OSIA login item: %s (%ld)\n", error.domain.UTF8String, (long)error.code);
            state = item.status;
        }
        if (state == SMAppServiceStatusRequiresApproval) {
            if (!automatic) [SMAppService openSystemSettingsLoginItems];
            return 3;
        }
        return state == SMAppServiceStatusEnabled ? 0 : 2;
    }
}
#endif

int main(int argc, char **argv) {
    char raw[PATH_MAX], executable[PATH_MAX], python[PATH_MAX], entry[PATH_MAX];
#ifdef __APPLE__
    if (argc == 3 && strcmp(argv[1], "--osia-login-item") == 0)
        return login_item(argv[2]);
    uint32_t size = sizeof raw;
    if (_NSGetExecutablePath(raw, &size) != 0) return 1;
    const char *resources = "/../Resources";
#else
    ssize_t count = readlink("/proc/self/exe", raw, sizeof raw - 1);
    if (count < 0 || count >= (ssize_t)sizeof raw - 1) return 1;
    raw[count] = '\0';
    const char *resources = "";
#endif
    if (!realpath(raw, executable)) return 1;
    char *slash = strrchr(executable, '/');
    if (!slash) return 1;
    *slash = '\0';
    if (snprintf(python, sizeof python, "%s%s/python/bin/python3.12", executable, resources) >= (int)sizeof python ||
        snprintf(entry, sizeof entry, "%s%s/portable.py", executable, resources) >= (int)sizeof entry) return 1;
    char **args = calloc((size_t)argc + 4, sizeof *args);
    if (!args) return 1;
    args[0] = python;
    args[1] = "-I";
    args[2] = "-B";
    args[3] = entry;
    for (int i = 1; i < argc; i++) args[i + 3] = argv[i];
    execv(python, args);
    perror("OSIA");
    return 1;
}
