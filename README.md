# Trabalho-redes

Comparação de protocolos de roteamento na mesma topologia Docker/FRR.

## Topologia

Os cinco roteadores formam quatro redes: `192.168.7.0/24`, `192.168.8.0/24`, `192.168.9.0/24` e `192.100.0.0/24`. A topologia fica em `docker-compose.yaml`; as configurações FRR ficam isoladas em `routers/rip/` e `routers/ospf/`. Assim, somente um protocolo é montado nos roteadores a cada execução.

## Executar RIP ou OSPF

Use o lançador Python a partir da raiz do repositório. Ele seleciona o arquivo `.env` correto, valida que os cinco roteadores possuem os três arquivos FRR e chama o Docker Compose.

```bash
docker build -t trabalho-redes-frr:local .

python3 run_topology.py rip up --build --detach

# Encerrar RIP antes de trocar de protocolo
python3 run_topology.py rip down

# Iniciar OSPF com a mesma topologia
python3 run_topology.py ospf up --build --detach
```

Outras ações disponíveis são `restart`, `logs` e `config`:

```bash
python3 run_topology.py ospf logs
python3 run_topology.py ospf config
```

Também é possível chamar o Compose diretamente, quando necessário:

```bash
docker compose --env-file .env.rip up --build
docker compose --env-file .env.ospf up --build
```

## Métricas e gráficos

O coletor mede, por roteador, rotas IPv4 instaladas, pacotes e bytes de controle, pacotes/s e bit/s. Também executa 20 pings em cada par de roteadores (intervalo padrão de 0,2 s) e registra RTT médio, mínimo, máximo, jitter (desvio padrão do RTT) e perda. Os pares são executados em paralelo, mas cada protocolo usa a mesma quantidade e intervalo. RTT mede delay fim a fim; ele não é uma medida de convergência.

Antes de coletar, inicie a topologia e espere o protocolo estabilizar:

```bash
python3 run_topology.py rip up --build --detach
docker exec router-a which tcpdump
python3 metrics/collect.py --protocol rip
```

Repita exatamente a mesma coleta após ativar cada configuração OSPF e do algoritmo próprio:

```bash
python3 metrics/collect.py --protocol ospf
python3 metrics/collect.py --protocol custom --custom-filter 'udp port 5200'
```

O filtro do último comando deve corresponder ao tráfego real do algoritmo próprio. `tcpdump` precisa estar disponível na imagem FRR. Os CSVs ficam em `metrics/results/` e são sobrescritos por uma nova execução do mesmo protocolo.

Para criar os gráficos, instale as dependências e execute:

```bash
python3 -m pip install -r requirements.txt
python3 metrics/plot.py
```

Os gráficos são salvos em `metrics/graphs/`. Eles incluem tabela de rotas, pacotes, bytes, bit/s, delay, jitter e perda. Para avaliar convergência, use um experimento separado: estabilize a rede, derrube um enlace e meça até uma rota alternativa voltar a funcionar.
