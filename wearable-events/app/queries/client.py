"""InfluxDB client construction, shared by every other query module."""
from influxdb_client import InfluxDBClient
from loguru import logger

from app.config import INFLUX_BUCKET, INFLUX_ORG, INFLUX_TOKEN, INFLUX_URL, SENSOR_MEASUREMENT

_client = None

def get_client() -> InfluxDBClient:
    ''' The single shared InfluxDBClient. Two defaults are overridden:

    enable_gzip - off by default in influxdb-client, meaning query
    responses come back as uncompressed annotated CSV. That format is
    extremely repetitive (every row repeats the full tag set), so it
    compresses several-fold; the CPU cost of decompressing is much
    smaller than the transfer it replaces, even over a local docker
    network.

    connection_pool_maxsize - defaults to cpu_count() * 5, i.e. 5-10
    connections on the small single-board machines this stack is meant
    to run on. Every route handler here is a plain `def`, so FastAPI
    runs them in its threadpool and a single page load can have half a
    dozen handlers querying concurrently; once that exceeds the pool,
    urllib3 opens and immediately discards extra connections, paying a
    fresh TCP handshake per query. 25 comfortably covers the app's own
    concurrency without being a meaningful resource cost.
    '''
    global _client
    if _client is None:
        _client = InfluxDBClient(
            url=INFLUX_URL,
            token=INFLUX_TOKEN,
            org=INFLUX_ORG,
            enable_gzip=True,
            connection_pool_maxsize=25,
        )
    return _client

def list_distinct_sensor_users(lookback_days: int = 365) -> list[str]:
    ''' Returns the distinct `user` tag values seen in the ring parser's
    sensor measurement over the lookback window. Used to power the
    registration "claim existing ring data" picker - a long default
    lookback (1 year) so someone whose ring hasn't synced in a while
    doesn't silently disappear from the list, at the cost of possibly
    surfacing a genuinely stale/abandoned identifier. Returns an empty
    list (not an error) if the bucket/measurement has no data yet, or if
    the query fails - callers should treat both the same way: fall back
    to manual entry.
    '''
    client = get_client()
    query_api = client.query_api()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -{lookback_days}d)
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> keep(columns: ["user"])
      |> distinct(column: "user")
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query distinct sensor users (bucket empty/unreachable?): {e}")
        return []

    users = []
    for table in tables:
        for record in table.records:
            value = record.get_value()
            if value:
                users.append(value)
    return sorted(set(users))