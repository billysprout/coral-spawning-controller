FROM python:3.12-slim

WORKDIR /app

# Pacific wall-clock in the containers, so local timestamps in the logs match
# the owner's history (datetime.now() is UTC otherwise). tzdata is not installed on python:3.12-slim by default.
# iputils-ping: the wedged-plug check pings a plug that stopped answering (tank_wedge.py).
# openssh-client: pi_log_archive.py, run as a child of tank_supervisor.py.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata iputils-ping openssh-client \
 && rm -rf /var/lib/apt/lists/*
ENV TZ=America/Los_Angeles

# CVE-2026-111943 (Oct 2026): eight eMudhra intermediate CAs had weak, instantly
# recoverable private keys, so anyone could mint certs under the emSign roots.
# Python's urllib reads this store and never sees Mozilla's OneCRL block. The
# stack only talks TLS to Telegram (GoDaddy), ntfy (Let's Encrypt) and Pushover
# (DigiCert), none of which chain to emSign, so distrust the emSign roots outright.
# The build fails if any survive. Remove this once Debian ships the distrust.
RUN sed -i 's#^mozilla/emSign_#!mozilla/emSign_#' /etc/ca-certificates.conf \
 && update-ca-certificates --fresh \
 && ! ls /etc/ssl/certs | grep -qi emsign

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# One image for the tank controller and the lifeguard; the service's command in
# docker-compose.yml picks the program. Every module lives in scripts/ and runs
# from there. cues.py reads cue_params.json beside it; the planner reads the
# year calendar from the data dir (./data mounted at /data), so regenerating the
# calendar (scripts/spawn_calendar.py) needs no rebuild.
COPY scripts/ ./scripts/

ENV TANK_DATA=/data \
    TANK_IN_CONTAINER=1

# No HEALTHCHECK here: each service defines its own in compose (data freshness).
CMD ["python", "-u", "scripts/tank_supervisor.py"]
