/* Linux-only, external fault injector; never included in a release bundle.
 * Stop immediately before ONE exact rename destination, after Python has
 * closed the temporary file. The harness observes this stop, then SIGKILLs
 * the process group. No artifact bytes or syscall return values are changed.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int rename(const char *source, const char *destination) {
    int (*real_rename)(const char *, const char *) = dlsym(RTLD_NEXT, "rename");
    if (!real_rename) _exit(125);
    const char *target = getenv("SIGIL_DRILL_RENAME_TARGET");
    const char *marker = getenv("SIGIL_DRILL_RENAME_MARKER");
    if (target && marker && strcmp(destination, target) == 0) {
        int fd = open(marker, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
        if (fd < 0) _exit(126);
        char pid[32];
        int length = snprintf(pid, sizeof(pid), "%ld\n", (long)getpid());
        if (write(fd, pid, length) != length || close(fd) != 0) _exit(127);
        raise(SIGSTOP);
        /* The harness must kill us, not resume and commit the write. */
        _exit(128);
    }
    return real_rename(source, destination);
}
