import logging
import os
from db import DB
import asyncio
import time
import pyarrow
from pyarrow import csv
import pyarrow.parquet as pq
from pandas import DataFrame
from datetime import datetime
from config import settings
from io import StringIO, BytesIO
from typing import Union
import boto3

logging.basicConfig(
    format = '[%(asctime)s] %(levelname)s [%(name)s:%(lineno)s] %(message)s',
    level = settings.LOG_LEVEL,
)

logger = logging.getLogger(__name__)
s3 = boto3.client("s3")
db = None

def get_database():
    global db
    if db is None:
        db = DB()
    return db

def reset_queue():
    """
    Initialize or reset the export log/queue. Update the queue
    """
    sql = """
    SELECT * FROM reset_export_logs();
    """
    db = get_database()
    return db.rows(sql, response_format='DataFrame')

def update_export_log(day: str, node: int):
    """Mark the location/day as exported"""
    if isinstance(day, str):
        day = datetime.fromisoformat(day).date()
    sql = """
    SELECT update_export_log_exported(:day, :node)
    """
    db = get_database()
    return db.rows(sql, day = day, node = node)

def get_all_location_days():
    """get the entire set of location/days."""

    sql = f"""
    SELECT sn.sensor_nodes_id
    , (m.datetime-'1sec'::interval)::date as day
    , COUNT(m.value) as n
    FROM measurements m
    JOIN sensors s ON (m.sensors_id = s.sensors_id)
    JOIN measurands p ON (s.measurands_id = p.measurands_id)
    JOIN sensor_systems ss ON (s.sensor_systems_id = ss.sensor_systems_id)
    JOIN sensor_nodes sn ON (ss.sensor_nodes_id = sn.sensor_nodes_id)
    LEFT JOIN versions v ON (s.sensors_id = v.sensors_id)
    WHERE s.sensors_id NOT IN (SELECT sensors_id FROM stale_versions)
    GROUP BY sn.sensor_nodes_id, (m.datetime-'1sec'::interval)::date
    LIMIT {settings.LIMIT}
    """
    db = get_database()
    return db.rows(sql, {});

def get_pending_location_days():
    """get the set of location/days that need to be updated."""
    sql = f"""
    SELECT * FROM get_pending({settings.LIMIT})
    """
    db = get_database()
    return db.rows(sql, {});


def get_measurement_data(
        sensor_nodes_id: int,
        day: Union[str, datetime.date],
):
    """
    Pull all measurement data for one site and day. Data is organized by sensor_node
    and the sensor_systems_id and units is appended to the measurand to ensure that
    there will be no duplicate columns when we convert to long format
    """
    if isinstance(day, str):
        day = datetime.fromisoformat(day).date()

    where = {
        'sensor_nodes_id': sensor_nodes_id,
        'day': day,
    }

    #AND (m.datetime - '1sec'::interval)::date = :day
    #, p.measurand||'-'||ss.sensor_systems_id||'-'||p.units as measurand
    sql = """
    SELECT *
    FROM measurement_data_export
    WHERE sensor_nodes_id = :sensor_nodes_id
    AND (datetime - '1sec'::interval)::date = :day
    --LIMIT 5
    """
    db = get_database()
    rows = db.rows(sql, **where, response_format='DataFrame');
    return rows;

def reshape(rows: Union[DataFrame, dict], fields: list = []):
    """Create a wide format dataframe from either records or a json/dict object from the database"""
    if len(rows) > 0:
        rows = rows[fields]
    return rows

def write_file(tbl, filepath: str = 'example'):
    """write the results in the given format"""
    #if not isinstance(tbl, pyarrow.lib.Table):
    #    tbl = convert(tbl)
    if settings.WRITE_FILE_FORMAT == 'csv':
        logger.debug('writing file to csv format')
        out = StringIO()
        ext = 'csv'
        mode = 'w'
        tbl.to_csv(out, index=False)
    elif settings.WRITE_FILE_FORMAT == 'csv.gz':
        logger.debug('writing file to csv.gx format')
        out = BytesIO()
        ext = 'csv.gz'
        mode = 'wb'
        tbl.to_csv(out, index=False, compression="gzip")
    elif settings.WRITE_FILE_FORMAT == 'parquet':
        logger.debug('writing file to parquet format')
        out = BytesIO()
        ext = 'parquet'
        mode = 'wb'
        tbl.to_parquet(out, index=False)
        #pq.write_table(tbl, )
    elif settings.WRITE_FILE_FORMAT == 'json':
        raise Exception("We are not supporting JSON yet")
    else:
        raise Exception(f"We are not supporting {settings.WRITE_FILE_FORMAT}")

    if (settings.WRITE_FILE_LOCATION == 's3'
        and settings.OPEN_DATA_BUCKET is not None
        and settings.OPEN_DATA_BUCKET != ''):
        logger.debug(f"writing file to bucket: {settings.OPEN_DATA_BUCKET}")
        s3.put_object(
             Bucket=settings.OPEN_DATA_BUCKET,
             Key=f"{filepath}.{ext}",
             Body=out.getvalue()
         )
    elif settings.WRITE_FILE_LOCATION == 'local':
        logger.debug(f"writing file to local file in {settings.LOCAL_SAVE_DIRECTORY}")
        filepath = os.path.join(settings.LOCAL_SAVE_DIRECTORY, filepath)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        txt = open(f"{filepath}.{ext}", mode)
        txt.write(out.getvalue())
        txt.close()
    else:
        raise Exception(f"{settings.WRITE_FILE_LOCATION} is not a valid location")


async def export_data(day, node, range = 'day'):
    try:
        start = time.time()
        rows=await get_measurement_data(
            sensor_nodes_id = node,
            day = day,
        )
        country = rows['country'][0]
        df = reshape(rows, fields = ["location","datetime","lat", "lon", "measurand", "value"])
        filepath = f"records/{settings.WRITE_FILE_FORMAT}/country={country}/locationid={node}/year={day.year}/month={day.month}/loc-{node}-{day.year}{day.month}{day.day}"
        write_file(df, filepath)
        await update_export_log(day, node)
    except Exception as e:
        logger.warning(f"Error processing {node}-{day}: {e}");
    finally:
        logger.info("export seconds: %0.4f", time.time() - start)

async def export_pending():
    """Only export the location/days that are marked for export. Location days will be limited to the value in the LIMIT environmental parameter"""
    start = time.time()
    days = await get_pending_location_days()
    for d in days:
        await export_data(d['day'], d['sensor_nodes_id'])
    logger.info(
        "export_pending: %s; seconds: %0.4f",
        len(days),
        time.time() - start,
    )
    return days

async def export_all():
    """Export all location/days in the database. This will reset the export log and then run the `export_pending` method."""
    await reset_export_log()
    return export_pending();

if __name__ == '__main__':
    #rsp = asyncio.run(export_all())
    #rsp = asyncio.run(reset_queue())
    #rsp = asyncio.run(get_pending_location_days())
    rsp = asyncio.run(export_pending())
    #rsp = asyncio.run(update_export_log('2021-08-08', 1))
    #print(rsp)
    print(f"total query time: {db.query_time}")
