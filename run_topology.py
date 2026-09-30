#!/usr/bin/env python3
"""Inicia a topologia Docker usando a configuração RIP ou OSPF escolhida."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROTOCOLS = ("rip", "ospf", "custom")
ROUTERS = ("a", "b", "c", "d", "e")


def compose_command():
    """Retorna o comando Compose disponível na máquina."""
    if shutil.which("docker"):
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    raise RuntimeError("Docker Compose não foi encontrado. Instale o Docker Compose e tente novamente.")


def validate_configuration(protocol):
    missing = []
    for router in ROUTERS:
        for filename in ("daemons", "frr.conf", "vtysh.conf"):
            path = ROOT / "routers" / protocol / router / filename
            if not path.is_file():
                missing.append(path.relative_to(ROOT))
    if missing:
        paths = "\n  ".join(str(path) for path in missing)
        raise RuntimeError(f"Configuração {protocol.upper()} incompleta:\n  {paths}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("protocol", choices=PROTOCOLS, help="protocolo de roteamento a usar")
    parser.add_argument("action", choices=("up", "down", "restart", "logs", "config"), help="ação do Docker Compose")
    parser.add_argument("--build", action="store_true", help="recria a imagem antes de iniciar (somente em up/restart)")
    parser.add_argument("-d", "--detach", action="store_true", help="executa em segundo plano (somente em up/restart)")
    args = parser.parse_args()

    try:
        validate_configuration(args.protocol)
        command = compose_command() + ["--env-file", f".env.{args.protocol}"]
        if args.action == "restart":
            subprocess.run(command + ["down"], cwd=ROOT, check=True)
            action = "up"
        else:
            action = args.action
        compose_args = [action]
        if action == "up":
            if args.build:
                compose_args.append("--build")
            if args.detach:
                compose_args.append("--detach")
        elif args.build or args.detach:
            parser.error("--build e --detach só podem ser usados com up ou restart")
        subprocess.run(command + compose_args, cwd=ROOT, check=True)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
