# Dockerfile for the Amazfit ("amazfit") parser image.
#
# Build context for this Dockerfile MUST be ./parser (the directory
# containing this file's parent), not ./parser/amazfit, since it
# needs COPY access to ../common/ as well as its own amazfit/ subtree:
#
#   docker build -f parser/amazfit/Dockerfile ./parser
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
    && pip install webdavclient3 influxdb-client loguru protobuf fitparse

# amazfit/app/huami_pb2.py is PRE-COMPILED (not generated at build
# time here) from amazfit/proto/huami.proto - Gadgetbridge's own
# protobuf schema for BASE_ACTIVITY_SUMMARY.RAW_SUMMARY_DATA (Zepp OS
# devices), confirmed against Gadgetbridge's real source (both the
# .proto itself and ZeppOsActivitySummaryParser.java, 2026-09). Kept
# pre-compiled and checked in rather than adding a protoc build step
# here, to keep this Dockerfile simple - if huami.proto ever changes,
# regenerate with (from parser/amazfit/):
#   protoc --python_out=app --proto_path=proto proto/huami.proto

# fitparse decodes per-workout .fit exports (BASE_ACTIVITY_SUMMARY's
# own RAW_DETAILS_PATH, found via Gadgetbridge's own "Auto export FIT
# tracks" automation - confirmed 2026-09 to work for GPS AND non-GPS
# activities alike, unlike GPX which only exists for GPS ones). Real,
# standardized format with a mature open-source parser - same
# reasoning as using the real protobuf library above, not a hand-
# rolled decoder.

COPY common /app/common
COPY amazfit/app /app
COPY amazfit/scripts /scripts
COPY common/entrypoint.sh /entrypoint.sh
RUN chmod +x /app/gadgetbridge_to_influxdb.py /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]