/* A deterministic native CLI fixture. No model, credentials or network calls. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/types.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <arpa/inet.h>

static void escaped(const char *value) {
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)value; *p; ++p) {
        if (*p == '"' || *p == '\\') { putchar('\\'); putchar(*p); }
        else if (*p < 32) printf("\\u%04x", *p);
        else putchar(*p);
    }
    putchar('"');
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--version")) { puts("codex-cli 0.0.0-fixture"); return 0; }
    if (argc == 2 && !strcmp(argv[1], "--stall-stdin")) { sleep(60); return 0; }
    if (argc == 2 && !strcmp(argv[1], "--child")) {
        pid_t child = fork();
        if (!child) { sleep(60); _exit(0); }
        sleep(60); return 0;
    }
    char input[32001] = {0}, message[16001] = {0};
    size_t used = fread(input, 1, 32000, stdin);
    input[used] = 0;
    if (!strcmp(input, "P2B_CPU_LIMIT")) { volatile unsigned long x = 0; while (1) { ++x; (void)x; } }
    if (!strcmp(input, "P2B_TIMEOUT")) { sleep(60); return 0; }
    if (!strcmp(input, "P2B_CLOSED_PIPES_TIMEOUT")) { close(1); close(2); sleep(60); return 0; }
    if (!strcmp(input, "P2B_STDOUT_OVERFLOW") || !strcmp(input, "P2B_STDERR_OVERFLOW")) {
        FILE *stream = strstr(input, "STDERR") ? stderr : stdout;
        for (int i = 0; i < 200000; ++i) fputc('x', stream);
        fflush(stream); return 0;
    }
    if (!strcmp(input, "P2B_FILE_LIMIT")) {
        FILE *stream = fopen("/runner/state/large-file", "w");
        if (!stream) return 3;
        for (int i = 0; i < 200000; ++i) fputc('x', stream);
        fflush(stream); return 3;
    }
    if (!strcmp(input, "P2B_NONZERO")) return 3;
    if (!strcmp(input, "P2B_INVALID_EVENTS")) { puts("not-json"); return 0; }
    if (!strcmp(input, "P2B_FAILED_EVENT")) { puts("{\"type\":\"turn.failed\"}"); return 0; }
    if (!strcmp(input, "NETWORK_CHECK")) {
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(80)};
        inet_pton(AF_INET, "192.0.2.1", &addr.sin_addr);
        strcpy(message, connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0 ? "DENIED" : "UNSAFE");
        close(fd);
    } else if (!strcmp(input, "WRITE_ROOT")) {
        int fd = open("/p2b-rootfile", O_CREAT | O_WRONLY, 0600);
        strcpy(message, fd < 0 ? "DENIED" : "UNSAFE");
        if (fd >= 0) close(fd);
    } else if (!strcmp(input, "RESOURCE_CHECK")) {
        struct rlimit mem, cpu, file, descriptors, core;
        getrlimit(RLIMIT_AS, &mem); getrlimit(RLIMIT_CPU, &cpu); getrlimit(RLIMIT_FSIZE, &file);
        getrlimit(RLIMIT_NOFILE, &descriptors); getrlimit(RLIMIT_CORE, &core);
        int bounded = mem.rlim_cur <= 2147483648UL && cpu.rlim_cur <= 300 && file.rlim_cur <= 65536
                      && descriptors.rlim_cur <= 128 && core.rlim_cur == 0;
        strcpy(message, bounded ? "BOUNDED" : "UNSAFE");
    } else if (!strcmp(input, "P2B_STATE_QUOTA")) {
        char name[100], block[32768] = {0};
        int bounded = 0;
        for (int i = 0; i < 600; ++i) {
            snprintf(name, sizeof(name), "/runner/state/file-%d", i);
            FILE *f = fopen(name, "w");
            if (!f) { bounded = 1; break; }
            if (fwrite(block, 1, sizeof(block), f) != sizeof(block) || fflush(f) != 0) bounded = 1;
            fclose(f);
            if (bounded) break;
        }
        strcpy(message, bounded ? "BOUNDED" : "UNSAFE");
    } else if (!strncmp(input, "READ:", 5)) {
        int fd = open(input + 5, O_RDONLY);
        if (fd < 0) strcpy(message, "DENIED");
        else { ssize_t got = read(fd, message, 16000); if (got >= 0) message[got] = 0; close(fd); }
    } else if (!strcmp(input, "WRITE_WORKSPACE")) {
        int fd = open("/workspace/safe.txt", O_WRONLY | O_TRUNC);
        strcpy(message, fd < 0 ? "DENIED" : "UNSAFE");
        if (fd >= 0) close(fd);
    } else if (!strcmp(input, "ENV_CHECK")) {
        const char *home = getenv("HOME"), *codex = getenv("CODEX_HOME");
        int safe = home && !strcmp(home, "/runner/state/home") && codex && !strcmp(codex, "/runner/state/codex");
        const char *blocked[] = {"MCP_BRIDGE_API_KEY", "CONTROL_PLANE_API_KEY", "AEE_MCP_FORWARD_AUTH",
                                 "CODEX_API_KEY", "OPENAI_API_KEY", "SSH_AUTH_SOCK", "LD_PRELOAD", NULL};
        for (int i = 0; blocked[i]; ++i) if (getenv(blocked[i])) safe = 0;
        strcpy(message, safe ? "SAFE" : "UNSAFE");
    } else if (!strcmp(input, "P2B_TRUNCATE")) {
        memset(message, 'x', 5000); message[5000] = 0;
    } else {
        snprintf(message, sizeof(message), "%s", input);
    }
    fputs("{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":", stdout);
    escaped(message);
    fputs("}}\n{\"type\":\"turn.completed\"}\n", stdout);
    return 0;
}
