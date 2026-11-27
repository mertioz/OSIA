/* A movable Finder/file-manager entry point for the bundled Python runtime. */
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif

int main(int argc, char **argv) {
    char raw[PATH_MAX], executable[PATH_MAX], python[PATH_MAX], entry[PATH_MAX];
#ifdef __APPLE__
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
