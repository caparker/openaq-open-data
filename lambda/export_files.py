# from open_data_export.db import DB
# from open_data_export.config import settings
import logging
import argparse
from open_data_export.config import settings
from open_data_export.main import export_data, export_pending
from datetime import datetime

logger = logging.getLogger(__name__)

logging.basicConfig(
    format='[%(asctime)s] %(levelname)s [%(name)s:%(lineno)s] %(message)s',
    level=settings.LOG_LEVEL.upper(),
    force=True,
)

parser = argparse.ArgumentParser(
    description="""
    Simple script to facilitate exporting data
    """)
parser.add_argument('--day', type=str, required=False,
                    help='The day to export, in YYYY-MM-DD')
parser.add_argument('--node', type=int, required=False,
                    help='The node id to export')
args = parser.parse_args()


if args.node and args.day:
	day = datetime.fromisoformat(args.day).date()
	export_data(day, args.node)
else:
	export_pending()
