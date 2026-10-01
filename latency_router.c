/*
 * Latency-based distance-vector routing daemon.
 *
 * Routes are exchanged as JSON over UDP.  The metric is the measured ICMP
 * round-trip time to the neighbour plus the metric it advertised.
 */
#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define LISTEN_PORT 5200
#define UPDATE_INTERVAL 5
#define PING_COUNT 3
#define PING_TIMEOUT 1
#define ROUTE_TIMEOUT 30
#define INFINITY_METRIC 99999.0
#define MAX_NETWORKS 4
#define MAX_NEIGHBORS 4
#define MAX_ROUTES 16
#define PREFIX_LEN 32
#define IP_LEN INET_ADDRSTRLEN
#define PAYLOAD_LEN 2048

typedef struct { const char *ip; const char *name; } Neighbor;
typedef struct {
    const char *hostname;
    const char *networks[MAX_NETWORKS];
    size_t network_count;
    Neighbor neighbors[MAX_NEIGHBORS];
    size_t neighbor_count;
} RouterConfig;

static const RouterConfig TOPOLOGY[] = {
    {"router-a", {"192.168.7.0/24", "192.168.9.0/24"}, 2,
     {{"192.168.7.2", "router-b"}, {"192.168.7.3", "router-c"},
      {"192.168.9.2", "router-d"}, {"192.168.9.3", "router-e"}}, 4},
    {"router-b", {"192.168.7.0/24", "192.100.0.0/24"}, 2,
     {{"192.168.7.1", "router-a"}, {"192.168.7.3", "router-c"},
      {"192.100.0.2", "router-e"}}, 3},
    {"router-c", {"192.168.7.0/24", "192.168.8.0/24"}, 2,
     {{"192.168.7.1", "router-a"}, {"192.168.7.2", "router-b"},
      {"192.168.8.2", "router-d"}}, 3},
    {"router-d", {"192.168.8.0/24", "192.168.9.0/24"}, 2,
     {{"192.168.8.1", "router-c"}, {"192.168.9.1", "router-a"},
      {"192.168.9.3", "router-e"}}, 3},
    {"router-e", {"192.168.9.0/24", "192.100.0.0/24"}, 2,
     {{"192.168.9.1", "router-a"}, {"192.168.9.2", "router-d"},
      {"192.100.0.1", "router-b"}}, 3},
};

typedef struct {
    char prefix[PREFIX_LEN];
    double metric;
    char next_hop[IP_LEN];
    time_t updated;
    bool local;
} Route;

typedef struct {
    const RouterConfig *config;
    Route routes[MAX_ROUTES];
    size_t route_count;
    pthread_mutex_t lock;
} Router;

static volatile sig_atomic_t running = 1;

static void log_line(const char *level, const char *fmt, ...) {
    time_t now = time(NULL);
    struct tm tm_now;
    char stamp[32];
    va_list args;
    localtime_r(&now, &tm_now);
    strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S", &tm_now);
    fprintf(stdout, "%s [%s] ", stamp, level);
    va_start(args, fmt);
    vfprintf(stdout, fmt, args);
    va_end(args);
    fputc('\n', stdout);
    fflush(stdout);
}

static void handle_signal(int signal_number) { (void)signal_number; running = 0; }

static const RouterConfig *find_config(const char *hostname) {
    for (size_t i = 0; i < sizeof(TOPOLOGY) / sizeof(TOPOLOGY[0]); i++)
        if (strcmp(hostname, TOPOLOGY[i].hostname) == 0) return &TOPOLOGY[i];
    return NULL;
}

static void get_router_hostname(char *hostname, size_t length) {
    FILE *file = fopen("/etc/frr/frr.conf", "r");
    char line[256], value[128];
    if (file != NULL) {
        while (fgets(line, sizeof(line), file) != NULL) {
            if (sscanf(line, "hostname %127s", value) == 1) {
                snprintf(hostname, length, "%s", value);
                fclose(file);
                return;
            }
        }
        fclose(file);
    }
    if (gethostname(hostname, length) != 0) snprintf(hostname, length, "unknown");
    hostname[length - 1] = '\0';
}

static bool is_local_network(const Router *router, const char *prefix) {
    for (size_t i = 0; i < router->config->network_count; i++)
        if (strcmp(prefix, router->config->networks[i]) == 0) return true;
    return false;
}

static double measure_latency(const char *ip) {
    char command[128], line[512];
    double minimum, average;
    FILE *pipe;
    snprintf(command, sizeof(command), "ping -c %d -W %d %s 2>/dev/null",
             PING_COUNT, PING_TIMEOUT, ip);
    pipe = popen(command, "r");
    if (pipe == NULL) return INFINITY_METRIC;
    while (fgets(line, sizeof(line), pipe) != NULL) {
        char *equals = strstr(line, " = ");
        if ((strstr(line, "rtt min/avg/max") || strstr(line, "round-trip min/avg/max")) &&
            equals != NULL && sscanf(equals + 3, "%lf/%lf/", &minimum, &average) == 2) {
            pclose(pipe);
            return average;
        }
    }
    pclose(pipe);
    return INFINITY_METRIC;
}

static void run_ip_route(const char *action, const char *prefix, const char *via) {
    pid_t child = fork();
    if (child == 0) {
        if (via != NULL) execlp("ip", "ip", "route", action, prefix, "via", via, (char *)NULL);
        else execlp("ip", "ip", "route", action, prefix, (char *)NULL);
        _exit(127);
    }
    if (child < 0) { log_line("WARNING", "could not start ip route: %s", strerror(errno)); return; }
    int status;
    if (waitpid(child, &status, 0) < 0 || !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        log_line("WARNING", "Route %s failed for %s", action, prefix);
    } else if (via != NULL) log_line("INFO", "Route set:     %-20s via %s", prefix, via);
    else log_line("INFO", "Route removed: %s", prefix);
}

static void install_route(const char *prefix, const char *via) { run_ip_route("replace", prefix, via); }
static void remove_route(const char *prefix) { run_ip_route("del", prefix, NULL); }

static void router_init(Router *router, const RouterConfig *config) {
    memset(router, 0, sizeof(*router));
    router->config = config;
    pthread_mutex_init(&router->lock, NULL);
    for (size_t i = 0; i < config->network_count; i++) {
        Route *route = &router->routes[router->route_count++];
        snprintf(route->prefix, sizeof(route->prefix), "%s", config->networks[i]);
        snprintf(route->next_hop, sizeof(route->next_hop), "local");
        route->updated = time(NULL);
        route->local = true;
    }
}

static Route *find_route(Router *router, const char *prefix) {
    for (size_t i = 0; i < router->route_count; i++)
        if (strcmp(router->routes[i].prefix, prefix) == 0) return &router->routes[i];
    return NULL;
}

static void consider_route(Router *router, const char *sender_ip, const char *prefix,
                           double neighbor_metric, double link_rtt) {
    if (is_local_network(router, prefix) || neighbor_metric >= INFINITY_METRIC) return;
    double candidate = link_rtt + neighbor_metric;
    pthread_mutex_lock(&router->lock);
    Route *current = find_route(router, prefix);
    if (current == NULL && router->route_count < MAX_ROUTES) {
        current = &router->routes[router->route_count++];
        snprintf(current->prefix, sizeof(current->prefix), "%s", prefix);
        current->metric = candidate;
        snprintf(current->next_hop, sizeof(current->next_hop), "%s", sender_ip);
        current->updated = time(NULL);
        current->local = false;
        pthread_mutex_unlock(&router->lock);
        install_route(prefix, sender_ip);
        return;
    }
    if (current != NULL && !current->local && candidate < current->metric) {
        current->metric = candidate;
        snprintf(current->next_hop, sizeof(current->next_hop), "%s", sender_ip);
        current->updated = time(NULL);
        pthread_mutex_unlock(&router->lock);
        install_route(prefix, sender_ip);
        return;
    }
    pthread_mutex_unlock(&router->lock);
}

/* Parses the small JSON shape emitted by make_snapshot: {"prefix":{"metric":N},...}. */
static void process_update(Router *router, const char *sender_ip, const char *json, double link_rtt) {
    const char *cursor = json;
    while ((cursor = strchr(cursor, '"')) != NULL) {
        char prefix[PREFIX_LEN];
        const char *end = strchr(cursor + 1, '"');
        const char *metric;
        double value;
        if (end == NULL || (size_t)(end - cursor - 1) >= sizeof(prefix)) break;
        memcpy(prefix, cursor + 1, (size_t)(end - cursor - 1));
        prefix[end - cursor - 1] = '\0';
        metric = strstr(end, "\"metric\":");
        if (metric == NULL || sscanf(metric + 9, "%lf", &value) != 1) break;
        consider_route(router, sender_ip, prefix, value, link_rtt);
        cursor = metric + 9;
    }
}

static void *listen_updates(void *argument) {
    Router *router = argument;
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    int reuse = 1;
    struct sockaddr_in address;
    if (sock < 0) { log_line("ERROR", "socket: %s", strerror(errno)); return NULL; }
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET; address.sin_addr.s_addr = htonl(INADDR_ANY);
    address.sin_port = htons(LISTEN_PORT);
    if (bind(sock, (struct sockaddr *)&address, sizeof(address)) < 0) {
        log_line("ERROR", "bind UDP port %d: %s", LISTEN_PORT, strerror(errno)); close(sock); return NULL;
    }
    while (running) {
        char buffer[PAYLOAD_LEN], sender_ip[IP_LEN];
        struct sockaddr_in sender; socklen_t sender_length = sizeof(sender);
        ssize_t received = recvfrom(sock, buffer, sizeof(buffer) - 1, 0,
                                    (struct sockaddr *)&sender, &sender_length);
        if (received < 0) { if (errno == EINTR) continue; log_line("ERROR", "recvfrom: %s", strerror(errno)); continue; }
        buffer[received] = '\0';
        if (inet_ntop(AF_INET, &sender.sin_addr, sender_ip, sizeof(sender_ip)) == NULL) continue;
        double rtt = measure_latency(sender_ip);
        log_line("INFO", "Update from %-15s RTT = %.2f ms", sender_ip, rtt);
        process_update(router, sender_ip, buffer, rtt);
    }
    close(sock); return NULL;
}

static void make_snapshot(Router *router, char *payload, size_t payload_size) {
    size_t used = 0;
    pthread_mutex_lock(&router->lock);
    used += (size_t)snprintf(payload + used, payload_size - used, "{");
    for (size_t i = 0; i < router->route_count && used < payload_size; i++)
        used += (size_t)snprintf(payload + used, payload_size - used, "%s\"%s\":{\"metric\":%.6f}",
                                 i == 0 ? "" : ",", router->routes[i].prefix, router->routes[i].metric);
    if (used < payload_size) snprintf(payload + used, payload_size - used, "}");
    pthread_mutex_unlock(&router->lock);
}

static void broadcast_table(Router *router) {
    char payload[PAYLOAD_LEN];
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) return;
    make_snapshot(router, payload, sizeof(payload));
    for (size_t i = 0; i < router->config->neighbor_count; i++) {
        struct sockaddr_in destination;
        memset(&destination, 0, sizeof(destination));
        destination.sin_family = AF_INET; destination.sin_port = htons(LISTEN_PORT);
        if (inet_pton(AF_INET, router->config->neighbors[i].ip, &destination.sin_addr) == 1)
            sendto(sock, payload, strlen(payload), 0, (struct sockaddr *)&destination, sizeof(destination));
    }
    close(sock);
}

static void expire_routes(Router *router) {
    time_t cutoff = time(NULL) - ROUTE_TIMEOUT;
    char expired[MAX_ROUTES][PREFIX_LEN]; size_t expired_count = 0;
    pthread_mutex_lock(&router->lock);
    for (size_t i = 0; i < router->route_count;) {
        if (!router->routes[i].local && router->routes[i].updated < cutoff) {
            snprintf(expired[expired_count++], PREFIX_LEN, "%s", router->routes[i].prefix);
            router->routes[i] = router->routes[--router->route_count];
        } else i++;
    }
    pthread_mutex_unlock(&router->lock);
    for (size_t i = 0; i < expired_count; i++) remove_route(expired[i]);
}

static void log_table(Router *router) {
    pthread_mutex_lock(&router->lock);
    log_line("INFO", "---- Routing table (%s) ----", router->config->hostname);
    for (size_t i = 0; i < router->route_count; i++)
        log_line("INFO", "  %-22s metric = %-8.2f %s", router->routes[i].prefix,
                 router->routes[i].metric, router->routes[i].local ? "[directly connected]" : router->routes[i].next_hop);
    pthread_mutex_unlock(&router->lock);
}

int main(void) {
    char hostname[128]; get_router_hostname(hostname, sizeof(hostname));
    const RouterConfig *config = find_config(hostname);
    if (config == NULL) { log_line("ERROR", "Hostname '%s' not found in topology", hostname); return EXIT_FAILURE; }
    signal(SIGINT, handle_signal); signal(SIGTERM, handle_signal);
    Router router; router_init(&router, config);
    log_line("INFO", "=== Latency-Based Routing Daemon — %s ===", hostname);
    pthread_t listener;
    if (pthread_create(&listener, NULL, listen_updates, &router) != 0) { log_line("ERROR", "could not create listener thread"); return EXIT_FAILURE; }
    for (unsigned int tick = 0; running; tick++) {
        broadcast_table(&router); expire_routes(&router);
        if (tick % 6 == 0) log_table(&router);
        for (int i = 0; i < UPDATE_INTERVAL && running; i++) sleep(1);
    }
    pthread_kill(listener, SIGINT); pthread_join(listener, NULL);
    pthread_mutex_destroy(&router.lock);
    return EXIT_SUCCESS;
}
