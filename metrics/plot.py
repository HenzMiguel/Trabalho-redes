#!/usr/bin/env python3
"""Gera gráficos comparativos a partir dos CSVs em metrics/results/."""

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

RESULTS = Path("metrics/results")
GRAPHS = Path("metrics/graphs")
PROTOCOLS = ("rip", "ospf", "custom")


def load(kind):
    frames = [pd.read_csv(path) for protocol in PROTOCOLS
              if (path := RESULTS / f"{protocol}_{kind}.csv").exists()]
    if not frames:
        raise FileNotFoundError(f"Nenhum CSV '*_{kind}.csv' foi encontrado em {RESULTS}.")
    return pd.concat(frames, ignore_index=True)


def bar(df, column, aggregation, title, ylabel, filename):
    grouped = df.groupby("protocol")[column].agg(aggregation).reindex(PROTOCOLS).dropna()
    if grouped.empty:
        print(f"Ignorando {filename}: não há valores válidos para {column}.")
        return
    ax = grouped.plot(kind="bar", color="#276fbf", rot=0)
    ax.set_title(title)
    ax.set_xlabel("Protocolo")
    ax.set_ylabel(ylabel)
    plt.tight_layout()
    plt.savefig(GRAPHS / filename, dpi=200)
    plt.close()


def main():
    GRAPHS.mkdir(parents=True, exist_ok=True)
    routers, pings = load("routers"), load("ping")
    bar(routers, "route_count", "mean", "Tamanho médio da tabela de roteamento", "Número médio de rotas", "routing_table_size.png")
    bar(routers, "routing_packets", "sum", "Pacotes de roteamento capturados", "Pacotes", "routing_packets.png")
    bar(routers, "routing_bytes", "sum", "Dados de controle transmitidos", "Bytes", "routing_bytes.png")
    bar(routers, "routing_bits_per_second", "mean", "Taxa média de transmissão do protocolo", "bit/s", "routing_bandwidth.png")
    bar(pings, "delay_avg_ms", "mean", "Delay médio fim a fim", "RTT médio (ms)", "delay.png")
    bar(pings, "jitter_ms", "mean", "Jitter médio", "Desvio padrão do RTT (ms)", "jitter.png")
    bar(pings, "packet_loss_percent", "mean", "Perda média de pacotes", "Perda (%)", "packet_loss.png")
    print("\nResumo de roteamento:")
    print(routers.groupby("protocol").agg(route_count=("route_count", "mean"), routing_packets=("routing_packets", "sum"), routing_bytes=("routing_bytes", "sum"), routing_bps=("routing_bits_per_second", "mean")).round(3))
    print("\nResumo de ping:")
    print(pings.groupby("protocol").agg(delay_ms=("delay_avg_ms", "mean"), jitter_ms=("jitter_ms", "mean"), packet_loss_percent=("packet_loss_percent", "mean")).round(3))
    print(f"\nGráficos salvos em {GRAPHS}")


if __name__ == "__main__":
    main()
