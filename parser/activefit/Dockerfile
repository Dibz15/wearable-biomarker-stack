# Dockerfile for the Amazfit ("activefit") parser image.
#
# Build context for this Dockerfile MUST be ./parser (the directory
# containing this file's parent), not ./parser/activefit, since it
# needs COPY access to ../common/ as well as its own activefit/ subtree:
#
#   docker build -f parser/activefit/Dockerfile ./parser
#
# NOTE: the image builds and runs, but extract_data() currently raises
# NotImplementedError on every sync - see README.md in this directory.
# Not meant to be deployed yet; this Dockerfile exists so CI/build
# plumbing is exercised and ready the moment extraction is filled in.

FROM python:slim-bullseye

RUN pip install --upgrade pip \
    && pip install webdavclient3 influxdb-client loguru

COPY common /app/common
COPY activefit/app /app
COPY activefit/scripts /scripts
COPY common/entrypoint.sh /entrypoint.sh
RUN chmod +x /app/gadgetbridge_to_influxdb.py /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
