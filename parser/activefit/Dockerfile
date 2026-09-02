# Dockerfile for the Amazfit ("activefit") parser image.
#
# Build context for this Dockerfile MUST be ./parser (the directory
# containing this file's parent), not ./parser/activefit, since it
# needs COPY access to ../common/ as well as its own activefit/ subtree:
#
#   docker build -f parser/activefit/Dockerfile ./parser
#
# NOTE: this image runs safely before the Active 3 Premium is even
# paired - every query gracefully skips a missing/mismatched table
# instead of crashing (see README.md in this directory), so it just
# quietly does nothing every cycle until the watch exists in the
# Gadgetbridge export. The HUAMI_* table/column names it queries are
# still best-effort guesses, not verified against real hardware -
# don't trust the data it produces as correct until you've gone
# through this directory's README.md checklist.

FROM python:slim-bullseye

RUN pip install --upgrade pip \
    && pip install webdavclient3 influxdb-client loguru

COPY common /app/common
COPY activefit/app /app
COPY activefit/scripts /scripts
COPY common/entrypoint.sh /entrypoint.sh
RUN chmod +x /app/gadgetbridge_to_influxdb.py /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]