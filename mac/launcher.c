/* BITCH.app's real program. macOS only grants camera and microphone access to a real, signed program, not a script,
   so this tiny launcher starts mac/run.sh as its child and waits. BITCH then shows up as "BITCH" in
   System Settings > Privacy & Security > Camera / Microphone. */
#include <spawn.h>
#include <stdlib.h>
#include <stdio.h>
#include <sys/wait.h>
extern char **environ;
int main(void) {
    char path[1024]; snprintf(path, sizeof path, "%s/BITCH-Robot/mac/run.sh", getenv("HOME"));
    char *argv[] = {"/bin/bash", path, NULL}; pid_t pid; int status = 0;
    if (posix_spawn(&pid, "/bin/bash", NULL, NULL, argv, environ) != 0) return 1;
    waitpid(pid, &status, 0);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
