#!/usr/bin/env python3
"""
Latency-Based Distance Vector Routing Daemon

Implements the Distance Vector routing algorithm using measured network
latency (RTT via ICMP ping) as the link metric, replacing RIP's hop count.

Algorithm:
  - Each router measures the RTT to its directly connected neighbors.
  - Routers exchange routing tables with neighbors via UDP broadcasts.
  - Route metric = accumulated RTT along the path (milliseconds).
  - Best path   = minimum total latency (Bellman-Ford).
"""

import json
import logging
import re
import socket
import subprocess
import sys
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

LISTEN_PORT     = 5200   # UDP port used to exchange routing tables
UPDATE_INTERVAL = 5      # seconds between routing broadcasts
PING_COUNT      = 3      # ICMP packets per latency measurement
PING_TIMEOUT    = 1      # seconds to wait per individual ping reply
ROUTE_TIMEOUT   = 30     # seconds before a stale route is removed
INFINITY        = 99999.0

# ── Topology ──────────────────────────────────────────────────────────────────
# Statically describes each router's directly connected networks and the IPs
# of its directly reachable neighbors (no routing needed to reach them).

TOPOLOGY = {
    'router-a': {
        'networks': ['192.168.7.0/24', '192.168.9.0/24'],
        'neighbors': [
            {'ip': '192.168.7.2', 'name': 'router-b'},
            {'ip': '192.168.7.3', 'name': 'router-c'},
            {'ip': '192.168.9.2', 'name': 'router-d'},
            {'ip': '192.168.9.3', 'name': 'router-e'},
        ],
    },
    'router-b': {
        'networks': ['192.168.7.0/24', '192.100.0.0/24'],
        'neighbors': [
            {'ip': '192.168.7.1', 'name': 'router-a'},
            {'ip': '192.168.7.3', 'name': 'router-c'},
            {'ip': '192.100.0.2', 'name': 'router-e'},
        ],
    },
    'router-c': {
        'networks': ['192.168.7.0/24', '192.168.8.0/24'],
        'neighbors': [
            {'ip': '192.168.7.1', 'name': 'router-a'},
            {'ip': '192.168.7.2', 'name': 'router-b'},
            {'ip': '192.168.8.2', 'name': 'router-d'},
        ],
    },
    'router-d': {
        'networks': ['192.168.8.0/24', '192.168.9.0/24'],
        'neighbors': [
            {'ip': '192.168.8.1', 'name': 'router-c'},
            {'ip': '192.168.9.1', 'name': 'router-a'},
            {'ip': '192.168.9.3', 'name': 'router-e'},
        ],
    },
    'router-e': {
        'networks': ['192.168.9.0/24', '192.100.0.0/24'],
        'neighbors': [
            {'ip': '192.168.9.1', 'name': 'router-a'},
            {'ip': '192.168.9.2', 'name': 'router-d'},
            {'ip': '192.100.0.1', 'name': 'router-b'},
        ],
    },
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_hostname():
    """Read the hostname from FRR's config file (set per-router in frr.conf)."""
    try:
        with open('/etc/frr/frr.conf') as f:
            for line in f:
                m = re.match(r'^hostname\s+(\S+)', line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return socket.gethostname()


def measure_latency(ip):
    """Return average RTT to *ip* in milliseconds, or INFINITY on failure."""
    try:
        result = subprocess.run(
            ['ping', '-c', str(PING_COUNT), '-W', str(PING_TIMEOUT), ip],
            capture_output=True,
            text=True,
            timeout=PING_COUNT * PING_TIMEOUT + 3,
        )
        # Handles both iputils format (rtt …) and BusyBox format (round-trip …)
        m = re.search(
            r'(?:rtt|round-trip) min/avg/max(?:/mdev)? = [\d.]+/([\d.]+)/',
            result.stdout,
        )
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return INFINITY


def set_kernel_route(network, via):
    """Install or replace a route in the kernel routing table."""
    try:
        subprocess.run(
            ['ip', 'route', 'replace', network, 'via', via],
            check=True,
            capture_output=True,
        )
        log.info('Route set:     %-20s  via %s', network, via)
    except subprocess.CalledProcessError as exc:
        log.warning('Route set failed for %s via %s: %s', network, via, exc)


def del_kernel_route(network):
    """Remove a route from the kernel routing table."""
    subprocess.run(['ip', 'route', 'del', network], capture_output=True)
    log.info('Route removed: %s', network)

# ── Router ────────────────────────────────────────────────────────────────────

class LatencyRouter:
    def __init__(self, hostname):
        cfg = TOPOLOGY[hostname]
        self.hostname   = hostname
        self.my_nets    = set(cfg['networks'])
        self.neighbors  = cfg['neighbors']

        # Routing table: prefix → {metric, next_hop, updated}
        # Directly connected networks start with metric 0.
        self._table = {
            net: {'metric': 0.0, 'next_hop': 'local', 'updated': time.time()}
            for net in self.my_nets
        }
        self._lock = threading.Lock()

    # ── Core algorithm ────────────────────────────────────────────────────────

    def _bellman_ford(self, sender_ip, neighbor_table, link_rtt):
        """
        Bellman-Ford step: for every prefix the neighbor advertises,
        check whether routing through it (link_rtt + neighbor_metric)
        is cheaper than our current best path.
        """
        with self._lock:
            now = time.time()
            for prefix, entry in neighbor_table.items():
                if prefix in self.my_nets:
                    continue
                nbr_metric = float(entry['metric'])
                if nbr_metric >= INFINITY:
                    continue

                new_metric = link_rtt + nbr_metric
                current    = self._table.get(prefix)

                if current is None or new_metric < current['metric']:
                    self._table[prefix] = {
                        'metric':   new_metric,
                        'next_hop': sender_ip,
                        'updated':  now,
                    }
                    set_kernel_route(prefix, sender_ip)

    def _expire_routes(self):
        """Remove routes that have not been refreshed within ROUTE_TIMEOUT."""
        with self._lock:
            cutoff  = time.time() - ROUTE_TIMEOUT
            expired = [
                p for p, e in self._table.items()
                if p not in self.my_nets and e['updated'] < cutoff
            ]
            for prefix in expired:
                del self._table[prefix]
                del_kernel_route(prefix)

    # ── Networking ────────────────────────────────────────────────────────────

    def _snapshot(self):
        """Return a JSON-serialisable copy of the routing table."""
        with self._lock:
            return {p: {'metric': e['metric']} for p, e in self._table.items()}

    def _broadcast(self):
        """Send our routing table to every directly connected neighbor."""
        payload = json.dumps(self._snapshot()).encode()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for nbr in self.neighbors:
            try:
                sock.sendto(payload, (nbr['ip'], LISTEN_PORT))
            except OSError:
                pass
        sock.close()

    def _listen(self):
        """Background thread: receive routing updates and run Bellman-Ford."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', LISTEN_PORT))
        sock.settimeout(1.0)

        while True:
            try:
                data, (sender_ip, _) = sock.recvfrom(65535)
                nbr_table = json.loads(data.decode())
                rtt = measure_latency(sender_ip)
                log.info('Update from %-15s  RTT = %.2f ms', sender_ip, rtt)
                self._bellman_ford(sender_ip, nbr_table, rtt)
            except socket.timeout:
                pass
            except Exception as exc:
                log.error('Listen error: %s', exc)

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log_table(self):
        with self._lock:
            log.info('──── Routing table (%s) ────', self.hostname)
            for prefix, entry in sorted(self._table.items()):
                if entry['next_hop'] == 'local':
                    log.info('  %-22s  metric = %-8.2f  [directly connected]', prefix, entry['metric'])
                else:
                    log.info('  %-22s  metric = %-8.2f  via %s', prefix, entry['metric'], entry['next_hop'])

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        log.info('=== Latency-Based Routing Daemon — %s ===', self.hostname)
        log.info('Local networks : %s', sorted(self.my_nets))
        log.info('Neighbors      : %s', [n['ip'] for n in self.neighbors])

        threading.Thread(target=self._listen, daemon=True).start()

        tick = 0
        while True:
            self._broadcast()
            self._expire_routes()
            if tick % 6 == 0:   # print table every ~30 s
                self._log_table()
            tick += 1
            time.sleep(UPDATE_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    hostname = get_hostname()
    if hostname not in TOPOLOGY:
        log.error(
            'Hostname "%s" not found in topology. Expected one of: %s',
            hostname, list(TOPOLOGY.keys()),
        )
        sys.exit(1)
    LatencyRouter(hostname).run()
