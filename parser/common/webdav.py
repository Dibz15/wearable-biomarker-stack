#!/usr/bin/env python3
"""
Shared across every device parser: downloading the Gadgetbridge SQLite
export over WebDAV and opening a connection to it.

Gadgetbridge maintains ONE database for every paired device (confirmed
against a real export - see common/devices.py) - so regardless of
which device(s) a given parser instance cares about, the fetch/open
step is identical. Nothing here should ever need to know about
COLMI_* vs HUAMI_* tables.
"""

import sys
import tempfile
from pathlib import Path

from loguru import logger


def fetch_database(webdav_client, webdav_path, export_file, local_filename="gadgetbridge.sqlite"):
    ''' Connect to the WebDAV server and fetch the named database
    file, if it exists.
    '''
    file_list = webdav_client.list(webdav_path)
    export_path = Path(webdav_path) / export_file
    if export_file in file_list:
        _ = webdav_client.info(str(export_path))
    else:
        logger.error(f"Error: Export file {export_path} does not exist")
        sys.exit(1)

    # Create a temporary directory to operate from
    tempdir = Path(tempfile.mkdtemp())

    # Download the file
    webdav_client.download_sync(remote_path=str(export_path), local_path=str(tempdir / local_filename))

    return tempdir


def open_database(tempdir, local_filename="gadgetbridge.sqlite"):
    ''' Open a handle on the database
    '''
    import sqlite3
    conn = sqlite3.connect(f"{tempdir}/{local_filename}")
    cur = conn.cursor()
    return conn, cur
