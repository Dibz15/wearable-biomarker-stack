# Dockerfile for the Colmi ring parser image.
#
# Build context for this Dockerfile MUST be ./parser (the directory
# containing this file's parent), not ./parser/colmi, since it needs
# COPY access to ../common/ as well as its own colmi/ subtree:
#
#   docker build -f parser/colmi/Dockerfile ./parser
#
# (CI does this via docker-publish.yml's `context: ./parser` +
# `file: ./parser/colmi/Dockerfile`.)
#
# Runs on a loop by default via common/entrypoint.sh so the image can
# run as a persistent service in a docker-compose stack, re-syncing
# every SYNC_INTERVAL_SECONDS. For one-shot behaviour (e.g. driven by
# an external cron/scheduler instead), override the entrypoint/command
# back to `/app/gadgetbridge_to_influxdb.py`.

FROM python:slim-bullseye

RUN pip install --upgrade pip \
    && pip install webdavclient3 influxdb-client loguru

COPY common /app/common
COPY colmi/app /app
COPY colmi/scripts /scripts
COPY common/entrypoint.sh /entrypoint.sh
RUN chmod +x /app/gadgetbridge_to_influxdb.py /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
