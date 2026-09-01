#!/usr/bin/env python3
"""
The Gadgetbridge DEVICE table and query-tolerance helper are shared
across every device parser:

- DEVICE (_id, NAME, IDENTIFIER, ALIAS) is populated by Gadgetbridge
  itself, once per paired device, regardless of device type - it's not
  a COLMI_* or HUAMI_* table. Every device-specific sample table joins
  back to it via a DEVICE_ID foreign key. Confirmed directly against a
  real export (see the DEVICE query below and its use in the original
  Colmi parser) rather than assumed.
- run_query()'s "tolerate a missing/unreadable table" behaviour isn't
  Colmi-specific either - any device parser may be pointed at an
  export from a Gadgetbridge version/build that doesn't populate every
  table it expects (e.g. a sensor the specific hardware lacks).
"""

import sqlite3

from loguru import logger


def run_query(cur, table_name, query) -> list | None:
    ''' Execute a query against one of the device sample tables,
    tolerating the table not existing (older/newer Gadgetbridge
    versions or firmware revisions may not populate every table).

    Returns a list of rows (possibly empty), or None if the query failed.
    '''
    try:
        res = cur.execute(query)
        rows = res.fetchall()
        logger.debug(f"{table_name}: query returned {len(rows)} row(s)")
        return rows
    except sqlite3.OperationalError as e:
        logger.warning(f"{table_name}: query failed ({e}) - skipping this table")
        return None


def fetch_devices(cur) -> dict | None:
    ''' Reads the DEVICE table into a {"dev-<id>": {name, identifier, alias}}
    dict. Returns None if the table is missing/unreadable (fatal for
    the caller - without it nothing can be tagged).
    '''
    device_query = "select _id, NAME, IDENTIFIER, ALIAS from DEVICE"
    device_rows = run_query(cur, "DEVICE", device_query)
    if device_rows is None:
        return None

    if not device_rows:
        logger.warning("DEVICE table returned zero rows - export may be from before initial pairing completed")

    devices = {}
    for r in device_rows:
        devices[f"dev-{r[0]}"] = {
            "name": r[1],
            "identifier": r[2],
            "alias": "Unset" if r[3] is None else r[3]
        }
    logger.info(f"Found {len(devices)} device(s) in export: "
                f"{[d['name'] for d in devices.values()]}")
    return devices


def device_tags_factory(devices):
    ''' Returns a device_tags(device_id) closure that degrades gracefully
    (with a warning) instead of raising KeyError if a sample references a
    device_id that isn't in the DEVICE table - can happen with stale/
    orphaned rows after a device is unpaired/re-paired.
    '''
    warned_devices = set()

    def device_tags(device_id):
        key = f"dev-{device_id}"
        if key not in devices:
            if device_id not in warned_devices:
                logger.warning(
                    f"Sample references unknown DEVICE_ID={device_id} "
                    f"(not present in DEVICE table) - tagging as 'unknown'. "
                    f"This will only be logged once per device_id."
                )
                warned_devices.add(device_id)
            return {
                "device": "unknown",
                "identifier": "unknown",
                "alias": "unknown"
            }
        d = devices[key]
        return {
            "device": d['name'],
            "identifier": d['identifier'],
            "alias": d['alias']
        }

    return device_tags
