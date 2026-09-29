#!/usr/bin/env python3
"""Coleta métricas comparáveis para RIP, OSPF e o protocolo próprio."""

import argparse
import csv
import re
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROUTERS = {"a": "router-a", "b": "router-b", "c": "router-c", "d": "router-d", "e": "router-e"}
# Um endereço de cada roteador, alcançável pelas demais redes da topologia.
ROUTER_IPS = {"a": "192.168.7.1", "b": "192.168.7.2", "c": "192.168.8.1", "d": "192.168.9.2", "e": "192.100.0.2"}
RESULTS = Path("metrics/results")


def docker_exec(container, *command, check=True):
    result = subprocess.run(["docker", "exec", container, *command], capture_output=True, text=True)
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "erro sem saída"
        raise RuntimeError(f"{container}: {detail}")
    return result.stdout


def routing_table_size(container):
    return sum(bool(line.strip()) for line in docker_exec(container, "sh", "-c", "ip -4 route").splitlines())


def ping_metrics(container, target, count, interval):
    # Código 1 é esperado quando há perda; o resumo do ping ainda é uma métrica válida.
    output = docker_exec(container, "ping", "-c", str(count), "-i", str(interval), target, check=False)
    rtts = [float(match.group(1)) for match in re.finditer(r"time[=<]([\d.]+)\s*ms", output)]
    loss_match = re.search(r"([\d.]+)% packet loss", output)
    loss = float(loss_match.group(1)) if loss_match else 100.0
    if not rtts:
        return {"delay_avg_ms": None, "delay_min_ms": None, "delay_max_ms": None,
                "jitter_ms": None, "packet_loss_percent": loss}
    return {"delay_avg_ms": statistics.mean(rtts), "delay_min_ms": min(rtts),
            "delay_max_ms": max(rtts), "jitter_ms": statistics.stdev(rtts) if len(rtts) > 1 else 0.0,
            "packet_loss_percent": loss}


def protocol_filter(protocol, custom_filter):
    if protocol == "custom":
        if not custom_filter:
            raise ValueError("Para custom, informe --custom-filter (ex.: 'udp port 9000').")
        return custom_filter
    return {"rip": "udp port 520", "ospf": "ip proto 89"}[protocol]


def ensure_tcpdump():
    missing = [name for name, container in ROUTERS.items()
               if not docker_exec(container, "sh", "-c", "command -v tcpdump", check=False).strip()]
    if missing:
        raise RuntimeError("tcpdump não está disponível nos roteadores: " + ", ".join(missing) + ". Instale-o na imagem FRR antes da coleta.")


def capture_protocol_traffic(capture_filter, duration):
    """Captura todos os roteadores em paralelo durante a mesma janela."""
    ensure_tcpdump()
    processes = {}
    for name, container in ROUTERS.items():
        processes[name] = subprocess.Popen(
            ["docker", "exec", container, "timeout", str(duration), "tcpdump", "-l", "-i", "any", "-n", "-q", capture_filter],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    captures = {}
    for name, process in processes.items():
        stdout, stderr = process.communicate(timeout=duration + 15)
        if process.returncode not in (0, 124):  # 124 é o término normal do timeout.
            raise RuntimeError(f"captura em router-{name} falhou: {stderr.strip()}")
        packets, total_bytes = 0, 0
        for line in stdout.splitlines():
            packets += 1
            match = re.search(r"length (\d+)", line)
            if match:
                total_bytes += int(match.group(1))
        captures[name] = {"routing_packets": packets, "routing_bytes": total_bytes,
                          "routing_packets_per_second": packets / duration,
                          "routing_bits_per_second": (total_bytes * 8) / duration}
    return captures


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Salvo: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True, choices=("rip", "ospf", "custom"))
    parser.add_argument("--duration", type=int, default=30, help="janela de captura em segundos (padrão: 30)")
    parser.add_argument("--ping-count", type=int, default=20, help="pings por par (padrão: 20)")
    parser.add_argument("--ping-interval", type=float, default=0.2, help="intervalo entre pings, em segundos (padrão: 0,2)")
    parser.add_argument("--custom-filter", help="filtro tcpdump do protocolo próprio; obrigatório para custom")
    args = parser.parse_args()
    if args.duration <= 0 or args.ping_count <= 0 or args.ping_interval <= 0:
        parser.error("--duration, --ping-count e --ping-interval devem ser positivos")
    try:
        capture_filter = protocol_filter(args.protocol, args.custom_filter)
        timestamp = datetime.now(timezone.utc).isoformat()
        print(f"Capturando {args.protocol.upper()} por {args.duration}s em todos os roteadores...")
        traffic = capture_protocol_traffic(capture_filter, args.duration)
        router_rows = [{"protocol": args.protocol, "router": name, "captured_at_utc": timestamp,
                        "capture_seconds": args.duration, "route_count": routing_table_size(container), **traffic[name]}
                       for name, container in ROUTERS.items()]
        ping_jobs = []
        for source, container in ROUTERS.items():
            for destination, address in ROUTER_IPS.items():
                if source != destination:
                    print(f"Ping {source.upper()} -> {destination.upper()}")
                    ping_jobs.append((source, destination, container, address))
        # Todos os pares usam o mesmo teste; executá-los em paralelo reduz a
        # coleta de minutos para segundos sem alterar count ou interval.
        with ThreadPoolExecutor(max_workers=len(ping_jobs)) as executor:
            futures = [(source, destination, executor.submit(ping_metrics, container, address, args.ping_count, args.ping_interval))
                       for source, destination, container, address in ping_jobs]
            ping_rows = [{"protocol": args.protocol, "source": source, "destination": destination,
                          "captured_at_utc": timestamp, "ping_count": args.ping_count,
                          "ping_interval_seconds": args.ping_interval, **future.result()}
                         for source, destination, future in futures]
        write_csv(RESULTS / f"{args.protocol}_routers.csv", router_rows)
        write_csv(RESULTS / f"{args.protocol}_ping.csv", ping_rows)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Erro na coleta: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
