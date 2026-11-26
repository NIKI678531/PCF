import logging
from enum import Enum
import pymssql
import os
from dotenv import load_dotenv

logger = logging.getLogger('Main')


class DB_NAME(Enum):
    RISK = "riskDb"
    OPS = "opsDb"
    PCFGEN = "pcfGenDb"


class ENV(Enum):
    DEV = "dev"
    PROD = "prod"


class DbConnection:
    def __init__(self, env: ENV):
        self.env_ = env
        load_dotenv()

    def GetConnection(self, db: DB_NAME) -> pymssql.Connection:
        # logger.info(f"Connecting to {db.value}.")
        dbconn = None
        if db == DB_NAME.RISK:
            host = os.getenv('risk_HOST_NAME')
            username = os.getenv('risk_USER_NAME')
            password = os.getenv('risk_PASS_WORD')
            database = os.getenv('risk_DATA_BASE')

        elif db == DB_NAME.OPS:
            host = os.getenv('ops_HOST_NAME')
            username = os.getenv('ops_USER_NAME')
            password = os.getenv('ops_PASS_WORD')
            database = os.getenv('ops_DATA_BASE')

        elif db == DB_NAME.PCFGEN:
            host = os.getenv('pcfgen_HOST_NAME')
            username = os.getenv('pcfgen_USER_NAME')
            password = os.getenv('pcfgen_PASS_WORD')
            database = os.getenv('pcfgen_DATA_BASE')

        try:
            dbconn = pymssql.connect(host, username, password, database, tds_version="7.0")
        except Exception as e:
            logger.exception(f"Error connecting to {db.value} database: {e}")
            raise e
        
        return dbconn
