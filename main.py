import time
import sys
import logging
from math import isfinite
from logging.handlers import TimedRotatingFileHandler
import schedule
from datetime import datetime, timedelta
import threading
from flask import Flask, request, jsonify
from flask_cors import CORS
import socket
import pandas as pd

from pcf import Pcf, PCF_STATUS, g_db_dateformat, g_riskdb_dateformat, g_opsdb_dateformat
from pcf_master import PcfMaster
from teamsmsger import TeamsMsger
from global_definition import g_lstFunds_am, g_lstFunds_pm

from db_connection import DbConnection, DB_NAME, ENV

pd.set_option('display.max_rows', None)
pd.set_option('display.max_columns', None)
pd.set_option('display.expand_frame_repr', False)

app = Flask(__name__)
CORS(app)

logger = logging.getLogger('Main')

def getPcfStatusByFileDate(filedate):
    global env

    pcfGenDbClnt = None
    pcf_status_list = []

    try:
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        sql = """
            select top 1 TradarCode, FileDate, Status, TimeStamp, Id, NavDate
            from t_pcf_status
            where FileDate = %s and TradarCode = %s
            order by Id desc
        """

        def append_pcf_status(cursor, tradarcode, session):
            cursor.execute(sql, (filedate, tradarcode))
            row = cursor.fetchone()

            if row is not None:
                pcf_status_list.append(
                    {
                        "TradarCode": row[0],
                        "FileDate": str(row[1]),
                        "Status": row[2],
                        "TimeStamp": str(row[3]),
                        "Id": row[4],
                        "NavDate": str(row[5]),
                        "session": session
                    }
                )
            else:
                pcf_status_list.append(
                    {
                        "TradarCode": tradarcode,
                        "FileDate": filedate,
                        "Status": PCF_STATUS.No.value,
                        "TimeStamp": None,
                        "Id": None,
                        "NavDate": None,
                        "session": session
                    }
                )

        with pcfGenDbClnt.cursor() as cursor:
            for funds, session in ((g_lstFunds_am, "am"), (g_lstFunds_pm, "pm")):
                for tradarcode in funds:
                    append_pcf_status(cursor, tradarcode, session)
                    
    except Exception as e:
        logger.exception(f"Error fetching PCF status for filedate {filedate}: {e}")
        raise
    finally:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

    return pcf_status_list



@app.route('/pcf_status', methods=['GET'])
def pcf_status():
    filedate = request.args.get('date')
    if not filedate:
        return jsonify({"error": "Missing required query parameter: date"}), 400

    try:
        datetime.strptime(filedate, "%Y%m%d")
    except ValueError:
        return jsonify({"error": "Invalid date format. Expected YYYYMMDD."}), 400

    try:
        pcf_status_list = getPcfStatusByFileDate(filedate)
 
        return jsonify(
            pcf_status_list,
        ), 200
    except Exception as e:
        logger.exception(f"Error in /pcf_status API: {e}")
        return jsonify({"error": "Internal server error"}), 500


@app.route('/gen_pcf', methods=['GET'])
def gen_pcf():
    tradar_code = request.args.get('tradar_code')
    filedate = request.args.get('date')

    if not tradar_code:
        return jsonify({"error": "Missing required query parameter: tradar_code"}), 400

    if not filedate:
        return jsonify({"error": "Missing required query parameter: date"}), 400

    try:
        datetime.strptime(filedate, "%Y%m%d")
    except ValueError:
        return jsonify({"error": "Invalid date format. Expected YYYYMMDD."}), 400

    try:
        global env
        db_connection = DbConnection(env)
        riskDbClnt = db_connection.GetConnection(DB_NAME.RISK)
        opsDbClnt = db_connection.GetConnection(DB_NAME.OPS)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        if tradar_code in g_lstFunds_am:
            batch = "AM"
        elif tradar_code in g_lstFunds_pm:
            batch = "PM"
        else:
            return jsonify({"error": f"Tradar code {tradar_code} not found in any fund list"}), 400
        
        pcf_master = PcfMaster([tradar_code], batch, filedate, riskDbClnt, opsDbClnt, pcfGenDbClnt, teams_msger, env, filedate)
        gen = pcf_master.GeneratePcf4Fund(tradar_code, filedate)

        if gen:
            return jsonify(
                {
                    "message": "PCF generation triggered",
                    "tradar_code": tradar_code,
                    "date": filedate,
                }
            ), 200
        else:
            return jsonify(
                {
                    "message": "PCF generation failed",
                    "tradar_code": tradar_code,
                    "date": filedate,
                }
            ), 500

    except Exception as e:
        logger.exception(f"Error in /gen_pcf API: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/pcf_config', methods=['GET'])
def get_pcf_config():
    try:
        global env
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        config_table = 't_pcf_config'
        sql = f"SELECT TradarCode, PcfPref, IOPVPcfPref, HSBCPcfPref, CSTNPcfPref FROM {config_table}"

        config_list = []
        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()

            for row in rows:
                clean_row = [value.strip() if isinstance(value, str) else value for value in row]
                config_list.append(
                    {
                        "TradarCode": clean_row[0],
                        "PcfPref": clean_row[1],
                        "IOPVPcfPref": clean_row[2],
                        "HSBCPcfPref": clean_row[3],
                        "CSTNPcfPref": clean_row[4]
                    }
                )

        return jsonify(config_list), 200
                    
    except Exception as e:
        logger.exception(f"Error in /pcf_config API: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/update_pcf_config', methods=['POST'])
def update_pcf_config():
    payload = request.get_json(silent=True)
    required_fields = ("TradarCode", "PcfPref", "IOPVPcfPref", "HSBCPcfPref", "CSTNPcfPref")

    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid JSON payload"}), 400

    missing_fields = [field for field in required_fields if field not in payload]
    if missing_fields:
        return jsonify({"error": "Missing required payload fields", "missing_fields": missing_fields}), 400

    tradar_code = payload["TradarCode"]
    
    pcfGenDbClnt = None
    try:
        global env
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute("DELETE FROM t_pcf_config WHERE TradarCode = %s", (tradar_code,))
            cursor.execute(
                """
                INSERT INTO t_pcf_config (TradarCode, PcfPref, IOPVPcfPref, HSBCPcfPref, CSTNPcfPref)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    tradar_code,
                    payload["PcfPref"],
                    payload["IOPVPcfPref"],
                    payload["HSBCPcfPref"],
                    payload["CSTNPcfPref"],
                ),
            )

        pcfGenDbClnt.commit()

        return jsonify({"message": "PCF config updated", "TradarCode": tradar_code}), 200

    except Exception as e:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.rollback()

        logger.exception(f"Error in /update_pcf_config API: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

@app.route('/fund_static', methods=['GET'])
def fund_static():
    pcfGenDbClnt = None
    try:
        global env
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        sql = """
            select TradarCode, Ticker, MIC, BasketSize, FeeRate, FeeFixed, Isin, BbgTicker, Ric, FundName
            from t_fund_statics
        """
        fund_static_list = []

        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()

            for row in rows:
                clean_row = [value.strip() if isinstance(value, str) else value for value in row]
                fund_static_list.append(
                    {
                        "TradarCode": clean_row[0],
                        "Ticker": clean_row[1],
                        "MIC": clean_row[2],
                        "BasketSize": clean_row[3],
                        "FeeRate": clean_row[4],
                        "FeeFixed": clean_row[5],
                        "Isin": clean_row[6],
                        "BbgTicker": clean_row[7],
                        "Ric": clean_row[8],
                        "FundName": clean_row[9]
                    }
                )

        return jsonify(fund_static_list), 200
    
    except Exception as e:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.rollback()

        logger.exception(f"Error in /fund_static API: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

@app.route('/update_fund_static', methods=['POST'])
def update_fund_static():
    payload = request.get_json(silent=True)
    required_fields = ("TradarCode", "Ticker")
    allowed_update_fields = {"MIC", "BasketSize", "FeeRate", "FeeFixed", "Isin", "BbgTicker", "Ric", "FundName"}

    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid JSON payload"}), 400

    update_fields = [field for field in payload if field not in required_fields]
    invalid_fields = [field for field in update_fields if field not in allowed_update_fields]
    if invalid_fields:
        return jsonify({"error": "Invalid update fields", "invalid_fields": invalid_fields}), 400

    if not update_fields:
        return jsonify({"error": "No update fields provided"}), 400

    tradar_code = payload["TradarCode"]
    ticker = payload["Ticker"]

    pcfGenDbClnt = None
    try:
        global env
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        set_clause = ", ".join([f"{field} = %s" for field in update_fields])
        values = [payload[field] for field in update_fields]
        values.extend([tradar_code, ticker])

        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(
                f"UPDATE t_fund_statics SET {set_clause} WHERE TradarCode = %s AND Ticker = %s",
                tuple(values),
            )

            if cursor.rowcount == 0:
                pcfGenDbClnt.rollback()
                return jsonify({"error": "Fund static row not found", "TradarCode": tradar_code, "Ticker": ticker}), 404

        pcfGenDbClnt.commit()

        return jsonify({"message": "Fund static updated", "TradarCode": tradar_code, "Ticker": ticker}), 200

    except Exception as e:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.rollback()

        logger.exception(f"Error in /update_fund_static API: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

@app.route('/pcf_holiday', methods=['GET'])
def pcf_holiday():
    tradar_code = request.args.get('TradarCode')
    if not tradar_code:
        return jsonify({"error": "Missing required query parameter: TradarCode"}), 400

    pcfGenDbClnt = None
    try:
        global env
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        sql = """
            select HolidayDate
            from t_pcf_holiday
            where TradarCode = %s
            order by HolidayDate asc
        """
        holiday_dates = []

        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql, (tradar_code,))
            rows = cursor.fetchall()

            for row in rows:
                holiday_dates.append(str(row[0]))

        return jsonify({"TradarCode": tradar_code, "HolidayDate": holiday_dates}), 200

    except Exception as e:
        logger.exception(f"Error in /pcf_holiday API: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

@app.route('/update_pcf_holiday', methods=['POST'])
def update_pcf_holiday():
    payload = request.get_json(silent=True)
    required_fields = ("TradarCode", "Date", "Action")

    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid JSON payload"}), 400

    missing_fields = [field for field in required_fields if field not in payload]
    if missing_fields:
        return jsonify({"error": "Missing required payload fields", "missing_fields": missing_fields}), 400

    tradar_codes = payload["TradarCode"]
    holiday_dates = payload["Date"]
    action = payload["Action"]

    if action not in ("Delete", "Add"):
        return jsonify({"error": "Invalid Action. Expected Delete or Add."}), 400

    if not isinstance(tradar_codes, list) or not tradar_codes:
        return jsonify({"error": "TradarCode must be a non-empty list"}), 400

    if not isinstance(holiday_dates, list) or not holiday_dates:
        return jsonify({"error": "Date must be a non-empty list"}), 400

    pcfGenDbClnt = None
    try:
        global env
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        affected_rows = 0
        with pcfGenDbClnt.cursor() as cursor:
            if action == "Delete":
                sql = "DELETE FROM t_pcf_holiday WHERE TradarCode = %s AND HolidayDate = %s"
                for tradar_code in tradar_codes:
                    for holiday_date in holiday_dates:
                        cursor.execute(sql, (tradar_code, holiday_date))
                        if cursor.rowcount > 0:
                            affected_rows += cursor.rowcount
            elif action == "Add":
                sql = "INSERT INTO t_pcf_holiday (TradarCode, HolidayDate) VALUES (%s, %s)"
                for tradar_code in tradar_codes:
                    for holiday_date in holiday_dates:
                        cursor.execute(sql, (tradar_code, holiday_date))
                        affected_rows += 1

        pcfGenDbClnt.commit()

        return jsonify(
            {
                "message": "Pcf holiday updated",
                "Action": action,
                "TradarCode": tradar_codes,
                "Date": holiday_dates,
                "AffectedRows": affected_rows
            }
        ), 200

    except Exception as e:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.rollback()

        logger.exception(f"Error in /update_pcf_holiday API: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

@app.route('/nav_holiday', methods=['GET'])
def nav_holiday():
    tradar_code = request.args.get('TradarCode')
    if not tradar_code:
        return jsonify({"error": "Missing required query parameter: TradarCode"}), 400

    pcfGenDbClnt = None
    try:
        global env
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        sql = """
            select HolidayDate
            from t_nav_holiday
            where TradarCode = %s
            order by HolidayDate asc
        """
        holiday_dates = []

        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql, (tradar_code,))
            rows = cursor.fetchall()

            for row in rows:
                holiday_dates.append(str(row[0]))

        return jsonify({"TradarCode": tradar_code, "HolidayDate": holiday_dates}), 200

    except Exception as e:
        logger.exception(f"Error in /nav_holiday API: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

@app.route('/update_nav_holiday', methods=['POST'])
def update_nav_holiday():
    payload = request.get_json(silent=True)
    required_fields = ("TradarCode", "Date", "Action")

    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid JSON payload"}), 400

    missing_fields = [field for field in required_fields if field not in payload]
    if missing_fields:
        return jsonify({"error": "Missing required payload fields", "missing_fields": missing_fields}), 400

    tradar_codes = payload["TradarCode"]
    holiday_dates = payload["Date"]
    action = payload["Action"]

    if action not in ("Delete", "Add"):
        return jsonify({"error": "Invalid Action. Expected Delete or Add."}), 400

    if not isinstance(tradar_codes, list) or not tradar_codes:
        return jsonify({"error": "TradarCode must be a non-empty list"}), 400

    if not isinstance(holiday_dates, list) or not holiday_dates:
        return jsonify({"error": "Date must be a non-empty list"}), 400

    pcfGenDbClnt = None
    try:
        global env
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        affected_rows = 0
        with pcfGenDbClnt.cursor() as cursor:
            if action == "Delete":
                sql = "DELETE FROM t_nav_holiday WHERE TradarCode = %s AND HolidayDate = %s"
                for tradar_code in tradar_codes:
                    for holiday_date in holiday_dates:
                        cursor.execute(sql, (tradar_code, holiday_date))
                        if cursor.rowcount > 0:
                            affected_rows += cursor.rowcount
            elif action == "Add":
                sql = "INSERT INTO t_nav_holiday (TradarCode, HolidayDate) VALUES (%s, %s)"
                for tradar_code in tradar_codes:
                    for holiday_date in holiday_dates:
                        cursor.execute(sql, (tradar_code, holiday_date))
                        affected_rows += 1

        pcfGenDbClnt.commit()

        return jsonify(
            {
                "message": "Nav holiday updated",
                "Action": action,
                "TradarCode": tradar_codes,
                "Date": holiday_dates,
                "AffectedRows": affected_rows
            }
        ), 200

    except Exception as e:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.rollback()

        logger.exception(f"Error in /update_nav_holiday API: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

@app.route('/pcf_fund', methods=['GET'])
def pcf_fund():
    global env

    tradar_code = request.args.get('TradarCode')
    pcf_date = request.args.get('PcfDate')

    if not tradar_code:
        return jsonify({"error": "Missing required query parameter: TradarCode"}), 400

    if not pcf_date:
        return jsonify({"error": "Missing required query parameter: PcfDate"}), 400

    try:
        datetime.strptime(pcf_date, "%Y%m%d")
    except ValueError:
        return jsonify({"error": "Invalid PcfDate format. Expected YYYYMMDD."}), 400

    # get the NAV date based of PCF date
    # fund_nav_date_dict = GetFundNavDate(g_lstFunds, env, pcf_date)
    
    # as_of_date = fund_nav_date_dict.get(tradar_code).replace("-", "")

    pcfGenDbClnt = None
    opsDbClnt = None
    try:
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)
        opsDbClnt = db_connection.GetConnection(DB_NAME.OPS)

        as_of_date = getExpectedNavDate(opsDbClnt, tradar_code, pcf_date, env)
        if as_of_date is None:
            return jsonify({"message": f"data for {tradar_code} at {pcf_date} and nav date = {as_of_date} is not ready."}), 200

        as_of_date = as_of_date.strftime("%Y%m%d")

        sql = """
            select TradarCode, NavPerShare, EstimateCash, TotalShares, FundCcy, AsOfDate
            from t_pcf_fund where TradarCode = %s and AsOfDate = %s
        """

        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql, (tradar_code, as_of_date))
            rows = cursor.fetchall()

            pcf_fund_list = []
            for row in rows:
                pcf_fund_list.append(
                    {
                        "TradarCode": row[0],
                        "NavPerShare": row[1],
                        "EstimateCash": row[2],
                        "TotalShares": row[3],
                        "FundCcy": row[4],
                        "AsOfDate": str(row[5])
                    }
                )

        return jsonify(pcf_fund_list), 200

    except Exception as e:
        logger.exception(f"Error in /pcf_fund API: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if opsDbClnt is not None:
            opsDbClnt.close()

        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

@app.route('/iopvpcf_fund', methods=['GET'])
def iopvpcf_fund():
    global env
    tradar_code = request.args.get('TradarCode')
    pcf_date = request.args.get('PcfDate')

    if not tradar_code:
        return jsonify({"error": "Missing required query parameter: TradarCode"}), 400

    if not pcf_date:
        return jsonify({"error": "Missing required query parameter: PcfDate"}), 400

    try:
        datetime.strptime(pcf_date, "%Y%m%d")
    except ValueError:
        return jsonify({"error": "Invalid PcfDate format. Expected YYYYMMDD."}), 400

    pcfGenDbClnt = None
    opsDbClnt = None
    try:
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)
        opsDbClnt = db_connection.GetConnection(DB_NAME.OPS)

        as_of_date = getExpectedNavDate(opsDbClnt, tradar_code, pcf_date, env)

        if as_of_date is None:
            return jsonify({"message": f"data for {tradar_code} at {pcf_date} and nav date = {as_of_date} is not ready."}), 200

        as_of_date = as_of_date.strftime("%Y%m%d")

        sql = """
            select TradarCode, NavPerShare, EstimateCash, TotalShares, FundCcy, AsOfDate
            from t_iopvpcf_fund where TradarCode = %s and AsOfDate = %s
        """

        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql, (tradar_code, as_of_date))
            rows = cursor.fetchall()

            iopvpcf_fund_list = []
            for row in rows:
                iopvpcf_fund_list.append(
                    {
                        "TradarCode": row[0],
                        "NavPerShare": row[1],
                        "EstimateCash": row[2],
                        "TotalShares": row[3],
                        "FundCcy": row[4],
                        "AsOfDate": str(row[5])
                    }
                )

        return jsonify(iopvpcf_fund_list), 200

    except Exception as e:
        logger.exception(f"Error in /iopvpcf_fund API: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if opsDbClnt is not None:
            opsDbClnt.close()

        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

@app.route('/pcf_holding', methods=['GET'])
def pcf_holding():
    global env
    tradar_code = request.args.get('TradarCode')
    pcf_date = request.args.get('PcfDate')

    if not tradar_code:
        return jsonify({"error": "Missing required query parameter: TradarCode"}), 400

    if not pcf_date:
        return jsonify({"error": "Missing required query parameter: PcfDate"}), 400

    try:
        datetime.strptime(pcf_date, "%Y%m%d")
    except ValueError:
        return jsonify({"error": "Invalid PcfDate format. Expected YYYYMMDD."}), 400

    pcfGenDbClnt = None
    opsDbClnt = None
    try:
        
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)
        opsDbClnt = db_connection.GetConnection(DB_NAME.OPS)

        as_of_date = getExpectedNavDate(opsDbClnt, tradar_code, pcf_date, env)

        if as_of_date is None:
            return jsonify({"message": f"data for {tradar_code} at {pcf_date} and nav date = {as_of_date} is not ready."}), 200

        as_of_date = as_of_date.strftime("%Y%m%d")

        sql = """
            select TradarCode, Ticker, Share, Price, Ccy, FxRate, Description, AsOfDate
            from t_pcf_holding where TradarCode = %s and AsOfDate = %s
        """

        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql, (tradar_code, as_of_date))
            rows = cursor.fetchall()

            pcf_holding_list = []
            for row in rows:
                pcf_holding_list.append(
                    {
                        "TradarCode": row[0],
                        "Ticker": row[1],
                        "Share": row[2],
                        "Price": row[3],
                        "Ccy": row[4],
                        "FxRate": row[5],
                        "Description": row[6],
                        "AsOfDate": str(row[7])
                    }
                )

        return jsonify(pcf_holding_list), 200

    except Exception as e:
        logger.exception(f"Error in /pcf_holding API: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if opsDbClnt is not None:
            opsDbClnt.close()

        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

@app.route('/iopvpcf_holding', methods=['GET'])
def iopvpcf_holding():
    global env
    tradar_code = request.args.get('TradarCode')
    pcf_date = request.args.get('PcfDate')

    if not tradar_code:
        return jsonify({"error": "Missing required query parameter: TradarCode"}), 400

    if not pcf_date:
        return jsonify({"error": "Missing required query parameter: PcfDate"}), 400

    try:
        datetime.strptime(pcf_date, "%Y%m%d")
    except ValueError:
        return jsonify({"error": "Invalid PcfDate format. Expected YYYYMMDD."}), 400

    pcfGenDbClnt = None
    opsDbClnt = None
    try:
        
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)
        opsDbClnt = db_connection.GetConnection(DB_NAME.OPS)

        as_of_date = getExpectedNavDate(opsDbClnt, tradar_code, pcf_date, env)
        if as_of_date is None:
            return jsonify({"message": f"data for {tradar_code} at {pcf_date} and nav date = {as_of_date} is not ready."}), 200

        as_of_date = as_of_date.strftime("%Y%m%d")

        sql = """
            select TradarCode, Ticker, Share, Price, Ccy, FxRate, Description, AsOfDate
            from t_iopvpcf_holding where TradarCode = %s and AsOfDate = %s
        """

        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql, (tradar_code, as_of_date))
            rows = cursor.fetchall()

            iopvpcf_holding_list = []
            for row in rows:
                iopvpcf_holding_list.append(
                    {
                        "TradarCode": row[0],
                        "Ticker": row[1],
                        "Share": row[2],
                        "Price": row[3],
                        "Ccy": row[4],
                        "FxRate": row[5],
                        "Description": row[6],
                        "AsOfDate": str(row[7])
                    }
                )

        return jsonify(iopvpcf_holding_list), 200

    except Exception as e:
        logger.exception(f"Error in /iopvpcf_holding API: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if opsDbClnt is not None:
            opsDbClnt.close()

        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

@app.route('/update_dividend', methods=['POST'])
def update_dividend():
    payload = request.get_json(silent=True)
    required_fields = ("Action", "Data")
    allowed_fields = {"Action", "Data"}

    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid JSON payload"}), 400

    missing_fields = [field for field in required_fields if field not in payload]
    if missing_fields:
        return jsonify({"error": "Missing required payload fields", "missing_fields": missing_fields}), 400

    invalid_fields = [field for field in payload if field not in allowed_fields]
    if invalid_fields:
        return jsonify({"error": "Invalid payload fields", "invalid_fields": invalid_fields}), 400

    action = payload["Action"]
    if isinstance(action, str):
        action = action.strip()

    if action not in ("Add", "Update", "Delete"):
        return jsonify({"error": "Invalid Action. Expected Add, Update, or Delete."}), 400

    dividend_items = payload["Data"]
    if not isinstance(dividend_items, list) or not dividend_items:
        return jsonify({"error": "Data must be a non-empty list"}), 400

    if action == "Add":
        # transform dividend items into the list of dictionaries
        lst_dividends = []
        for item in dividend_items:
            tradar_code = item.get("TradarCode")
            ex_date = item.get("ExDate")
            dividend = item.get("Dividend")
            div_ccy = item.get("DivCcy")

            lst_dividends.append({
                "TradarCode": tradar_code,
                "ExDate": ex_date,
                "Dividend": dividend,
                "DivCcy": div_ccy
            })

        # now persist lst_dividends to the database
        try:
            db_connection = DbConnection(env)
            pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with pcfGenDbClnt.cursor() as cursor:
                for dividend in lst_dividends:
                    sql = f"""
                        insert into t_etf_div (TradarCode, ExDate, Dividend, DivCcy, UpdatedOn)
                        values ('{dividend['TradarCode']}', '{dividend['ExDate']}', {dividend['Dividend']}, '{dividend['DivCcy']}', '{timestamp}')
                    """
                    cursor.execute(sql)
                pcfGenDbClnt.commit()
            return jsonify({"message": "Dividends added successfully"}), 200
        except Exception as e:
            if pcfGenDbClnt is not None:
                pcfGenDbClnt.rollback()
            logger.exception(f"Error in /update_dividend API: {e}")
            return jsonify({"error": "Internal server error"}), 500
        finally:
            if pcfGenDbClnt is not None:
                pcfGenDbClnt.close()

    if action == "Delete":
            # transform dividend items into the list of dictionaries
            lst_dividends = []
            for item in dividend_items:
                tradar_code = item.get("TradarCode")
                ex_date = item.get("ExDate")
    
                lst_dividends.append({
                    "TradarCode": tradar_code,
                    "ExDate": ex_date,
                })
    
            # now persist lst_dividends to the database
            try:
                db_connection = DbConnection(env)
                pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)
    
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                with pcfGenDbClnt.cursor() as cursor:
                    for dividend in lst_dividends:
                        sql = f"delete from t_etf_div where TradarCode = '{dividend['TradarCode']}' and ExDate = '{dividend['ExDate']}'"
                        cursor.execute(sql)
                    pcfGenDbClnt.commit()
                return jsonify({"message": "Dividends deleted successfully"}), 200
            except Exception as e:
                if pcfGenDbClnt is not None:
                    pcfGenDbClnt.rollback()
                logger.exception(f"Error in /update_dividend API: {e}")
                return jsonify({"error": "Internal server error"}), 500
            finally:
                if pcfGenDbClnt is not None:
                    pcfGenDbClnt.close()

    if action == "Update":
            # transform dividend items into the list of dictionaries
            lst_dividends = []
            for item in dividend_items:
                tradar_code = item.get("TradarCode")
                ex_date = item.get("ExDate")
                dividend = item.get("Dividend")

                lst_dividends.append({
                    "TradarCode": tradar_code,
                    "ExDate": ex_date,
                    "Dividend": dividend,
                })
    
            # now persist lst_dividends to the database
            try:
                db_connection = DbConnection(env)
                pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)
    
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                with pcfGenDbClnt.cursor() as cursor:
                    for dividend in lst_dividends:
                        sql = f"update t_etf_div set Dividend = {dividend['Dividend']} where TradarCode = '{dividend['TradarCode']}' and ExDate = '{dividend['ExDate']}'"
                        cursor.execute(sql)
                    pcfGenDbClnt.commit()
                return jsonify({"message": "Dividends updated successfully"}), 200
            except Exception as e:
                if pcfGenDbClnt is not None:
                    pcfGenDbClnt.rollback()
                logger.exception(f"Error in /update_dividend API: {e}")
                return jsonify({"error": "Internal server error"}), 500
            finally:
                if pcfGenDbClnt is not None:
                    pcfGenDbClnt.close()

   
@app.route('/get_dividend', methods=['GET'])
def get_dividend():
    global env
    
    pcfGenDbClnt = None
    try:
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        sql = 'select TradarCode, ExDate, Dividend, DivCcy from t_etf_div order by ExDate desc'

        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()

            dividend_list = []
            for row in rows:
                dividend_list.append(
                    {
                        "TradarCode": row[0],
                        "ExDate": str(row[1]),
                        "Dividend": row[2],
                        "DivCcy": row[3]
                    }
                )
        return jsonify(dividend_list), 200

    except Exception as e:
        logger.exception(f"Error in /get_dividend API: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

@app.route('/get_targetexpo', methods=['GET'])
def get_targetexpo():
    global env
        
    pcfGenDbClnt = None
    try:
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        sql = 'select TradarCode, TargetExpo from t_target_expo order by TradarCode'

        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()

            expo_list = []
            for row in rows:
                expo_list.append(
                    {
                        "TradarCode": row[0],
                        "TargetExpo": str(row[1])
                    }
                )
        return jsonify(expo_list), 200

    except Exception as e:
        logger.exception(f"Error in /get_targetexpo API: {e}")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

@app.route('/update_targetexpo', methods=['POST'])
def update_targetexpo():
    payload = request.get_json(silent=True)
    if not isinstance(payload, list) or not payload:
        return jsonify({"error": "Payload must be a non-empty list"}), 400

    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            return jsonify({"error": "Each item must be an object", "index": index}), 400

        missing_fields = [field for field in ("TradarCode", "TargetExpo") if field not in item]
        if missing_fields:
            return jsonify({
                "error": "Missing required item fields",
                "index": index,
                "missing_fields": missing_fields,
            }), 400

        if not isinstance(item["TradarCode"], str) or not item["TradarCode"].strip():
            return jsonify({"error": "TradarCode must be a non-empty string", "index": index}), 400

        target_expo = item["TargetExpo"]
        if (
            isinstance(target_expo, bool)
            or not isinstance(target_expo, (int, float))
            or (isinstance(target_expo, float) and not isfinite(target_expo))
        ):
            return jsonify({"error": "TargetExpo must be a finite number", "index": index}), 400

    pcfGenDbClnt = None
    try:
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        with pcfGenDbClnt.cursor() as cursor:
            for item in payload:
                cursor.execute(
                    "update t_target_expo set TargetExpo = %s where TradarCode = %s",
                    (item["TargetExpo"], item["TradarCode"]),
                )
            pcfGenDbClnt.commit()
        return jsonify({"message": "Target exposures updated successfully"}), 200
    except Exception as e:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.rollback()
        logger.exception(f"Error in /update_targetexpo API: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

# using after cutoff time, to check any pending PCFs.
def refreshPcfStatus(pcfGenDbClnt, pcf_master, pcf_produce_date_ymd):

    pending_tradar_codes = []
    for pcf in pcf_master.lstPcfs_:

        tradar_code = pcf.tradar_code_
        filedate = pcf.pcf_file_date_

        if pcf.IsPcfHoliday(pcf_produce_date_ymd): 
            logger.info(f"refreshPcfStatus: {tradar_code} is PCF holiday on {pcf_produce_date_ymd}")
            continue

        sql = f"select top 1 TradarCode, Status from t_pcf_status where FileDate = '{filedate}' and TradarCode = '{tradar_code}' order by Id desc"
    
        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
            
            for row in rows:
                Status = row[1]
                if Status == PCF_STATUS.Pending.value:
                    tradar_code = row[0]
                    pending_tradar_codes.append(tradar_code)

    return pending_tradar_codes

def getCdbFundId(opsDbClnt, tradar_code):
    with opsDbClnt.cursor() as cursor:
        # if tradar_code end with "_X", it is a listed share class. For now, only case is SG-WGBI_X, skip it, because will get CdbFundId for SG-WGBI, they have same cdbfundid 
        if tradar_code.endswith("_X"):
            tradar_code_key = tradar_code[:-2]
        else:
            tradar_code_key = tradar_code
            
        cursor.execute(
            f"SELECT Fid FROM t_FDE_Tradar_FundMap WHERE MasterClass = 1 AND TradarFundCode = '{tradar_code_key}'"
        )
        fid_row = cursor.fetchone()
        if fid_row is None:
            msg = f"No Fid found for TradarCode {tradar_code}"
            logger.error(msg)
            raise ValueError(msg)
        fid = fid_row[0]

        cursor.execute(
            f"SELECT CdbFundId FROM t_FDE_Cdb_FundMap WHERE Fid = {fid}"
        )
        cdb_row = cursor.fetchone()
        if cdb_row is None:
            msg = f"No CdbFundId found for Fid {fid} (TradarCode {tradar_code})"
            logger.error(msg)
            raise ValueError(msg)

        return cdb_row[0]

def getPcfHolidays(tradar_code, env):
    pcfGenDbClnt = None
    try:
        db_connection = DbConnection(env)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)

        sql = "select HolidayDate from t_pcf_holiday where TradarCode = %s"
        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql, (tradar_code,))
            rows = cursor.fetchall()

        return [
            row[0].strftime(g_opsdb_dateformat) if hasattr(row[0], "strftime") else str(row[0])
            for row in rows
        ]
    finally:
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()

def getExpectedNavDate(opsDbClnt, tradar_code, pcf_date, env):
    cdb_fund_id = getCdbFundId(opsDbClnt, tradar_code)
    pcf_holidays = getPcfHolidays(tradar_code, env)
    return Pcf.ExpectedNavDate(opsDbClnt, cdb_fund_id, pcf_date, tradar_code, pcf_holidays)

def RunPcfGeneration(lstFunds_am, lstFunds_pm, env, teams_msger):
    db_connection = DbConnection(env)

    riskDbClnt = None
    opsDbClnt = None
    pcfGenDbClnt = None

    start_time_am = "08:20"
    cutoff_time_am = "09:40" # 09:40, 

    start_time_pm = "22:00"  # 22:00, 
    cutoff_time_pm = "23:00" # 23:00,  

    parsed_start_am_dt = datetime.strptime(start_time_am, "%H:%M")
    parsed_cutoff_am_dt = datetime.strptime(cutoff_time_am, "%H:%M")

    start_hour_am = parsed_start_am_dt.hour
    start_minute_am = parsed_start_am_dt.minute
    cutoff_hour_am = parsed_cutoff_am_dt.hour
    cutoff_minute_am = parsed_cutoff_am_dt.minute

    parsed_start_pm_dt = datetime.strptime(start_time_pm, "%H:%M")
    parsed_cutoff_pm_dt = datetime.strptime(cutoff_time_pm, "%H:%M")
    
    start_hour_pm = parsed_start_pm_dt.hour
    start_minute_pm = parsed_start_pm_dt.minute
    cutoff_hour_pm = parsed_cutoff_pm_dt.hour
    cutoff_minute_pm = parsed_cutoff_pm_dt.minute


    pcf_master_am = None
    pcf_master_pm = None

    try:
        riskDbClnt = db_connection.GetConnection(DB_NAME.RISK)
        opsDbClnt = db_connection.GetConnection(DB_NAME.OPS)
        pcfGenDbClnt = db_connection.GetConnection(DB_NAME.PCFGEN)
        
        while True:
            # this part should be inside the loop, it will run to tomorrow. now() return includes the date
            current_time = datetime.now()
            # current_time = datetime.strptime("2026-11-20 22:40:00", "%Y-%m-%d %H:%M:%S") # e.g. Morning Fund, target nav date = 20261020, set to 20261021, 

            # skip satureday and sunday, TimDbg: 
            if current_time.weekday() >= 5:
                time.sleep(30)
                # logger.info("Skipping weekend.")
                continue

            start_time_am = current_time.replace(hour=start_hour_am, minute=start_minute_am, second=0, microsecond=0)
            cutoff_time_am = current_time.replace(hour=cutoff_hour_am, minute=cutoff_minute_am, second=0, microsecond=0)
            start_time_pm = current_time.replace(hour=start_hour_pm, minute=start_minute_pm, second=0, microsecond=0)
            cutoff_time_pm = current_time.replace(hour=cutoff_hour_pm, minute=cutoff_minute_pm, second=0, microsecond=0)
            
            pcf_produce_date_ymd = current_time.strftime(g_db_dateformat) # date when the PCF is going to be produced

            if current_time > cutoff_time_pm: # PM cutoff time
                if pcf_master_pm is None:
                    pcf_master_pm = PcfMaster(lstFunds_pm, 'PM', pcf_produce_date_ymd, riskDbClnt, opsDbClnt, pcfGenDbClnt, teams_msger, env)
                pending_tradar_codes = refreshPcfStatus(pcfGenDbClnt, pcf_master_pm, pcf_produce_date_ymd)
                if len(pending_tradar_codes) > 0:
                    title = f"PM PCF Status Cutoff - ({current_time.strftime('%Y-%m-%d %H:%M:%S')})"
                    msg = ", ".join(pending_tradar_codes) + " Pending"
                    logger.info(f"TeamsMessage: PM PCF Cutoff {msg}")
                    teams_msger.sendMessage(msg, title)
                else:
                    title = f"PM PCF Status Cutoff - ({current_time.strftime('%Y-%m-%d %H:%M:%S')})"
                    msg = "All PM PCFs are generated successfully."
                    logger.info(f"TeamsMessage: PM PCF Cutoff {msg}")
                    teams_msger.sendMessage(msg, title)

                # calculate the seconds to sleep until 00:01 tomorrow, system will sleep after cutoff time
                tomorrow = current_time + timedelta(days=1)
                midnight = datetime.combine(tomorrow.date(), datetime.min.time())
                seconds_until_midnight = (midnight - current_time).total_seconds() + 60 # + 60 make sure next while loop will be next day
                time.sleep(seconds_until_midnight)

            elif current_time > start_time_pm: # begin PM pcf generation
                if pcf_master_pm is None:
                    pcf_master_pm = PcfMaster(lstFunds_pm, 'PM', pcf_produce_date_ymd, riskDbClnt, opsDbClnt, pcfGenDbClnt, teams_msger, env)
                else:
                    if pcf_produce_date_ymd != pcf_master_pm.GetPcfProduceDate():
                        # it is a new day, create a new PcfMaster with the new pcf_date_pm
                        logger.info(f"Creating new PcfMaster PM instance for new date: {pcf_produce_date_ymd}") # this is necessary, because pcf date is initialized when pcf object is created, when it is next day, need to create the pcf object again so the the pcf date is correct.
                        pcf_master_pm = PcfMaster(lstFunds_pm, 'PM', pcf_produce_date_ymd, riskDbClnt, opsDbClnt, pcfGenDbClnt, teams_msger, env)

                pcf_master_pm.GenerateAllPcf()

            elif current_time > cutoff_time_am: # AM cutoff time
                if pcf_master_am is None:
                    # for start the server after cutoff time, PcfMaster is not created.
                    pcf_master_am = PcfMaster(lstFunds_am, 'AM', pcf_produce_date_ymd, riskDbClnt, opsDbClnt, pcfGenDbClnt, teams_msger, env)
                                
                pending_tradar_codes = refreshPcfStatus(pcfGenDbClnt, pcf_master_am, pcf_produce_date_ymd)
                if len(pending_tradar_codes) > 0:
                    title = f"AM PCF Status Cutoff - ({current_time.strftime('%Y-%m-%d %H:%M:%S')})"
                    msg = ", ".join(pending_tradar_codes) + " Pending"
                    logger.info(f"TeamsMessage: AM PCF Cutoff {msg}")
                    teams_msger.sendMessage(msg, title)
                else:
                    title = f"AM PCF Status Cutoff - ({current_time.strftime('%Y-%m-%d %H:%M:%S')})"
                    msg = "All AM PCFs are generated successfully."
                    logger.info(f"TeamsMessage: AM PCF Cutoff {msg}")
                    teams_msger.sendMessage(msg, title)

                # calculate the seconds to sleep until PM Start
                seconds_until_pm_start = (start_time_pm - current_time).total_seconds() + 5 # + 5 seconds make sure next while loop will be after PM start time
                if seconds_until_pm_start > 0:
                    time.sleep(seconds_until_pm_start)

            elif current_time > start_time_am: # begin AM pcf generation
                if pcf_master_am is None:
                    pcf_master_am = PcfMaster(lstFunds_am, 'AM', pcf_produce_date_ymd, riskDbClnt, opsDbClnt, pcfGenDbClnt, teams_msger, env)
                else:
                    # if pcf_date != pcf_master_am.GetPcfDate():
                    if pcf_produce_date_ymd != pcf_master_am.GetPcfProduceDate():
                        # it is a new day, create a new PcfMaster with the new pcf_date
                        logger.info(f"Creating new PcfMaster AM instance for new date: {pcf_produce_date_ymd}")
                        pcf_master_am = PcfMaster(lstFunds_am, 'AM', pcf_produce_date_ymd, riskDbClnt, opsDbClnt, pcfGenDbClnt, teams_msger, env)

                pcf_master_am.GenerateAllPcf()
            
            time.sleep(30)  # Wait for 30 seconds before the next try

    except Exception as e:
        logger.exception(f"Error during PCF generation: {e}")
    finally:
        if opsDbClnt is not None:
            opsDbClnt.close()
            opsDbClnt = None
        if riskDbClnt is not None:
            riskDbClnt.close()
            riskDbClnt = None
        if pcfGenDbClnt is not None:
            pcfGenDbClnt.close()
            pcfGenDbClnt = None

        

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None
    
if __name__ == "__main__":

    logFileFullName = r'c:/temp/t13/PcfGenLog.txt'
    
    logger.setLevel(level=logging.DEBUG)

    # StreamHandler
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(level=logging.DEBUG)
    streamformatter = logging.Formatter('%(asctime)s-%(levelname)s: %(message)s')
    stream_handler.setFormatter(streamformatter)
    logger.addHandler(stream_handler)

    # FileHandler TimedRotating
    file_handler = TimedRotatingFileHandler(logFileFullName, encoding='utf-8', when='midnight', interval=1, backupCount=100)
    file_handler.setLevel(level=logging.INFO)
    formatter = logging.Formatter('%(asctime)s-%(levelname)s: %(message)s')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.info("****************************")
    logger.info('Program Start ...')

    local_ip = get_local_ip()
    if local_ip and local_ip.startswith('192.168.152'):
        portno = 5005
        env = ENV.DEV
    else:
        portno = 5005
        env = ENV.PROD

    teams_msger = TeamsMsger(env)

    # Run once immediately, then continue running on the schedule.
    # RunPcfGeneration(g_lstFunds, env, teams_msger)

    delay_seconds = 10  # Adjust delay as needed
    timer = threading.Timer(
        delay_seconds,
        lambda: threading.Thread(target=RunPcfGeneration, args=(g_lstFunds_am, g_lstFunds_pm, env, teams_msger), daemon=True).start(),
    )
    timer.start()

    # Run the Flask app
    app.run(host='0.0.0.0', port=portno, debug=False)