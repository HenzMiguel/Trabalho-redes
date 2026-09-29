FROM frrouting/frr:latest

# A imagem oficial usada neste projeto é Alpine Linux. O coletor de métricas
# depende de tcpdump; ping já vem incluído na imagem base.
USER root
RUN apk add --no-cache tcpdump
