from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from enum import Enum
import logging
import sys
import shutil
import os

logger = logging.getLogger('Main')

g_db_dateformat = '%Y%m%d'
g_riskdb_dateformat = '%Y-%m-%d'
g_opsdb_dateformat = '%Y-%m-%d'



class PcfType(Enum):
    PCF = "PCF"
    IOPVPCF = "IOPVPCF"
    HSBCPCF = "HSBCPCF"
    CSTNPCF = "CSTNPCF"


class PCF_STATUS(Enum):
    Pending = "Pending"
    Done = "Done"
    Revise = "Revise"
    No = "No" # not operation

class Pcf(ABC):
    def __init__(self, tradar_code: str, cdb_fund_id: str, pcf_date: str, pcfGenDbClnt, opsDbClnt):
        self.tradar_code_ = tradar_code
        self.fund_name_ = None
        self.bbgticker_ = None
        self.reuterscode_ = None
        self.cdb_fund_id_ = cdb_fund_id
        self.cdb_listed_class_id_ = self._masterClassId(opsDbClnt) # master class also the listed share class
        self.isin_ = None
        # self.pcf_file_date_ = datetime.strptime(pcf_date, g_db_dateformat)
        self.pcf_file_date_ = pcf_date
        self.basket_size_ = None

        self.nav_per_share_ = None
        self.nav2_per_share_ = None # used in 7709, equity call, calc estimate cash
        self.estimated_cash_ = None
        self.total_shares_ = None
        self.iopv_estimated_cash_ = None
        self.total_net_assets_ = None

        self.pcf_files_ = [] # store the generated PCF file full names, e.g. pcf, iopvpcf, hsbc file, etc
        self.pcf_holidays_ = []

        self._init_fund_info(pcfGenDbClnt) # pay attentino to the location of this call, inside will fill pcf_holidays_
        self.fund_ccy_ = None # will be set in SyncComponents

        # debug for a NAV date
        # self.debugmode_ = False
        # self.debug_nav_date_ = datetime.strptime("20260528", g_db_dateformat)

        self.expected_nav_date_ = Pcf.ExpectedNavDate(opsDbClnt, cdb_fund_id, pcf_date, tradar_code, self.pcf_holidays_)
        # logger.info(f"Expected NAV date for {self.tradar_code_} is {self.expected_nav_date_}")
    
    def _GetPMATradarCode(self, opsDbClnt):
        ticker = None
        if self.tradar_code_.endswith("_X"):
            tradarcode = self.tradar_code_[:-2] # convert SG-WGBI_X to SG-WGBI
            if tradarcode == 'SG-WGBI':
                ticker = 'CYX'
            # else:
            # can add other ticker for other fund with multiple listed share class here
        else:
            tradarcode = self.tradar_code_
        return tradarcode, ticker

    def _masterClassId(self, opsDbClnt) -> int:
        # SG-WGBI_X -> SG-WGBI, all other cases will just return self.tradar_code_ and None for ticker
        tradarcode, ticker = self._GetPMATradarCode(opsDbClnt)

        if ticker is not None:
            # Only SG-WGBI_X case
            id_sql = f"SELECT cdb.CdbFundId, cdb.CdbClassId, tfm.TradarFundCode, tfm.MasterClass FROM t_FDE_Tradar_FundMap AS tfm JOIN t_FDE_Cdb_FundMap AS cdb ON cdb.Fid = tfm.Fid WHERE TradarFundCode = '{tradarcode}' and ticker = '{ticker}'"

            with opsDbClnt.cursor() as cursor:
                cursor.execute(id_sql)
                row = cursor.fetchall()
                if len(row) != 1:
                    logger.error(f"Use Ticker to get CdbFundId and CdbClassId for {self.tradar_code_}, should have only one row")
                    logger.info(f"Sql: {id_sql}")
                    sys.exit(1)
                else:
                    return row[0][1] # return the CdbClassId
        else:
            id_sql = f"SELECT cdb.CdbFundId, cdb.CdbClassId, tfm.TradarFundCode, tfm.MasterClass FROM t_FDE_Tradar_FundMap AS tfm JOIN t_FDE_Cdb_FundMap AS cdb ON cdb.Fid = tfm.Fid WHERE TradarFundCode = '{tradarcode}'"

        # get the CdbFundId and CdbClassId for the TradarCode from opsDbClnt
        with opsDbClnt.cursor() as cursor:
            cursor.execute(id_sql)
            row = cursor.fetchall()
            if len(row) > 1:
                # unlisted share classes exist for this fund
                for r in row:
                    if r[3] == 1: # MasterClass = 1
                        return r[1] # return the CdbClassId of the master class
            elif len(row) == 1:
                return row[0][1] # return the CdbClassId
            else:
                logger.error(f"No Cdb FundId and Classid found for {self.tradar_code_}. Exiting.")
                logger.info(f"Sql: {id_sql}")
                sys.exit(1)

    def _init_fund_info(self, pcfGenDbClnt):
        sql = f"SELECT FundName, BbgTicker, Ric, Isin, BasketSize FROM t_fund_statics WHERE TradarCode = '{self.tradar_code_}'"
        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()
            if row is None:
                logger.error(f"No fund info found for tradar_code {self.tradar_code_}. Exiting.")
                sys.exit(1)
            self.fund_name_ = row[0]
            self.bbgticker_ = row[1]
            self.reuterscode_ = row[2]
            self.isin_ = row[3]
            self.basket_size_ = row[4]

            holiday_sql = f"SELECT HolidayDate FROM t_pcf_holiday WHERE TradarCode = '{self.tradar_code_}'"
            with pcfGenDbClnt.cursor() as cursor:
                cursor.execute(holiday_sql)
                rows = cursor.fetchall()
                self.pcf_holidays_ = [row[0] for row in rows]

    def IsPcfHoliday(self, pcf_produce_date_ymd):
        return pcf_produce_date_ymd in self.pcf_holidays_

    def getExpectedNavDate(self):
        return self.expected_nav_date_
          

    @classmethod
    def ExpectedNavDate(cls, opsDbClnt, cdb_fund_id, pcf_date: str, tradar_code: str, lstHolidays: list[str]):
        pcf_as_of_date = datetime.strptime(pcf_date, g_db_dateformat)

        # for T0 fund (morning), pcf_date is today, for T1 fund(night), pcf_date is T+1 day
        # for both case, NAV date is pcf_date - 1 day
        expected_nav_date = pcf_as_of_date - timedelta(days=1)

        while expected_nav_date.weekday() >= 5 or expected_nav_date.strftime(g_db_dateformat) in lstHolidays:
            expected_nav_date -= timedelta(days=1)
        # now expected_nav_date skips saturday, sunday and holidays

        expected_nav_date_str = expected_nav_date.strftime(g_opsdb_dateformat)
        recon_type = 'NAV1' # we will use NAV1 to determine the nav date 

        nav_holiday_sql = f"SELECT is_nav_holiday FROM fund_level_data WHERE as_of_date = '{expected_nav_date_str}' AND acct_book_id = '{cdb_fund_id}' and recon_type = '{recon_type}'"

        with opsDbClnt.cursor() as cursor:
            cursor.execute(nav_holiday_sql)
            row = cursor.fetchone()
            if row is None:
                logger.warning(f"Cannot find NAV1 for {tradar_code} as of date {expected_nav_date_str}. Check NAV late or missing holiday entry.")
                logger.info(f"NAV1 Sql: {nav_holiday_sql}")
                expected_nav_date = None
                return expected_nav_date
            else:
                is_nav_holiday = row[0]
                
                while is_nav_holiday == 1:
                    expected_nav_date -= timedelta(days=1)
                    while expected_nav_date.weekday() >= 5 or expected_nav_date.strftime(g_opsdb_dateformat) in lstHolidays: # skip sat, sun and holidays
                        expected_nav_date -= timedelta(days=1)

                    expected_nav_date_str = expected_nav_date.strftime(g_opsdb_dateformat)
                    nav_holiday_sql = f"SELECT is_nav_holiday FROM fund_level_data WHERE as_of_date = '{expected_nav_date_str}' AND acct_book_id = '{cdb_fund_id}' and recon_type = '{recon_type}'"
                    cursor.execute(nav_holiday_sql)
                    row = cursor.fetchone()
                    if row is None:
                        logger.error(f"Impossible, ops forgot to signoff holiday some days before")
                        sys.exit(1)
                    is_nav_holiday = row[0]
      
                # self.expected_nav_date_ = expected_nav_date
                return expected_nav_date

    # this should be same for all PCF types.
    def _SyncPcfData(self, riskDbClnt, opsDbClnt, pcfGenDbClnt) -> bool:

        # I don't need to verify the expected_nav_date now because it is from OpsDB. It must be correct.
        return self._SyncPcfFundData(riskDbClnt, opsDbClnt, pcfGenDbClnt, self.expected_nav_date_) # inside call _SyncPcfComponents()
        

    def _GenPcfFileName(self, pcfGenDbClnt) -> dict:
        # get data from pcfGenDB.t_pcf_config table by key = TradarCode
        sql = f"select PcfPref, IOPVPcfPref, HSBCPcfPref, CSTNPcfPref from t_pcf_config where TradarCode = '{self.tradar_code_}'"
        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()
            if row is None:
                logger.error(f"No PCF config for {self.tradar_code_} in t_pcf_config. Exiting.")
                logger.info(f"Sql: {sql}")
                sys.exit(1)
        
        pcf_pref = row[0]
        iopv_pcf_pref = row[1]
        hsbc_pcf_pref = row[2]
        cstn_pcf_pref = row[3]

        pcf_files = {}
        if pcf_pref is not None and pcf_pref != "":
            file_name = f"{pcf_pref}{self.pcf_file_date_}.csv"
            pcf_files[PcfType.PCF.value] = file_name
        
        if iopv_pcf_pref is not None and iopv_pcf_pref != "":
            file_name = f"{iopv_pcf_pref}{self.pcf_file_date_}.csv"
            pcf_files[PcfType.IOPVPCF.value] = file_name

        if hsbc_pcf_pref is not None and hsbc_pcf_pref != "":
            file_name = f"{hsbc_pcf_pref}{self.pcf_file_date_}A.txt"
            pcf_files[PcfType.HSBCPCF.value] = file_name

        if cstn_pcf_pref is not None and cstn_pcf_pref != "":
            file_name = f"{cstn_pcf_pref}{self.pcf_file_date_}.csv"
            pcf_files[PcfType.CSTNPCF.value] = file_name
        
        return pcf_files

    @abstractmethod
    def _SyncPcfComponents(self, riskDbClnt, pcfGenDbClnt, as_of_date: datetime) -> bool:
        ...

    @abstractmethod
    def _SyncPcfFundData(self, riskDbClnt, opsDbClnt, pcfGenDbClnt, as_of_date: datetime) -> bool:
        ...

    @abstractmethod
    def GeneratePcf(self, riskDbClnt, opsDbClnt, pcfGenDbClnt, pcf_full_path: str, pcf_release_path: str, release=True, force=False) -> None:
        ...
    
    def calculateWeight(self, riskDbClnt, opsDbClnt, as_of_date) -> float:
        listed_classid = self.cdb_listed_class_id_
        # previous, hardcode NAV2, check
        sql = f"SELECT pma_nav FROM fund_level_data WHERE as_of_date = '{as_of_date}' AND acct_book_id = '{self.cdb_fund_id_}' and acct_book_class_id = '{listed_classid}' and recon_type = '{self.RECON_TYPE}'"

        with opsDbClnt.cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()
            if row is None:
                logger.error(f"Cannot find NAV for listed share class {listed_classid} of {self.tradar_code_} on {as_of_date}. Exiting.")
                logger.info(f"Sql: {sql}")
                sys.exit(1)
            
            listed_nav = float(row[0])
        
        # then find the total nav of the fund
        holding_sql = f"select acct_holding_id from account_holdings where acct_book_id = '{self.cdb_fund_id_}' and as_of_date = '{as_of_date}' and holding_type = '{self.RECON_TYPE}'"
        with riskDbClnt.cursor() as cursor:
            cursor.execute(holding_sql)
            row = cursor.fetchone()
            if row is None:
                logger.error(f"Cannot find holding for {self.tradar_code_} on {as_of_date}. Exiting.")
                logger.info(f"Sql: {holding_sql}")
                sys.exit(1)
            
            acct_holding_id = row[0]

        # Note: here we don't find the unlisted nav, instead we find total nav
        total_nav_sql = f"select sum(value_fc) from account_holdings_data where acct_holding_id = '{acct_holding_id}'"
        
        with riskDbClnt.cursor() as cursor:
            cursor.execute(total_nav_sql)
            row = cursor.fetchone()
            if row is None:
                logger.error(f"Cannot find total NAV for {self.tradar_code_} on {as_of_date}. Exiting.")
                logger.info(f"Sql: {total_nav_sql}")
                sys.exit(1)
            
            total_nav = float(row[0])
            self.total_net_assets_ = total_nav

        weight = listed_nav / total_nav
        logger.info(f"Listed weight of {self.tradar_code_} on {as_of_date} = {listed_nav} / {total_nav} = {weight}")
        return weight

    def ComponentListWeight(self, riskDbClnt, opsDBClnt, asofdate) -> float:
        # riskDbClnt used to get the holding of the fund
        # opsDBClnt used to find whether these is unlisted shared class
        weight = 1.0

        # SG-WGBI_X -> SG-WGBI, all other cases will just return self.tradar_code_ and None for ticker
        tradarcode, _ = self._GetPMATradarCode(opsDBClnt)

        multi_shared_sql = f"SELECT cdb.CdbFundId, cdb.CdbClassId, tfm.TradarFundCode, tfm.MasterClass FROM t_FDE_Tradar_FundMap AS tfm JOIN t_FDE_Cdb_FundMap AS cdb ON cdb.Fid = tfm.Fid WHERE TradarFundCode = '{tradarcode}'"
        # get the CdbFundId and CdbClassId for the TradarCode from opsDbClnt
        with opsDBClnt.cursor() as cursor:
            cursor.execute(multi_shared_sql)
            row = cursor.fetchall()
            if len(row) > 1:
                # unlisted share classes exist for this fund
                weight = self.calculateWeight(riskDbClnt, opsDBClnt, asofdate)
            elif len(row) == 1:
                return weight # weight = 1 at this case
            else:
                logger.error(f"No CdbFundId found for {self.tradar_code_}. Exiting.")
                logger.info(f"Sql: {multi_shared_sql}")
                sys.exit(1)

        return weight

    def ReleasePcfFiles(self, release_path: str):
        for pcf_file in self.pcf_files_:
            filename = os.path.basename(pcf_file)
            release_full_name = os.path.join(release_path, filename)
            shutil.copy(pcf_file, release_full_name)
            logger.info(f"Released PCF file: {release_full_name}")

    def _getETFOutDividend(self, opsDbClnt, pcfGenDbClnt):
            ex_date_str = self.pcf_file_date_

            div_tbl = 't_etf_div'
            sql_div = f"select Dividend, DivCcy from {div_tbl} where TradarCode = '{self.tradar_code_}' and ExDate = '{ex_date_str}'"

            with pcfGenDbClnt.cursor() as cursor:
                cursor.execute(sql_div)
                row = cursor.fetchone()
                if row is not None:
                    dividend = float(row[0])
                    div_ccy = row[1]
                else:
                    dividend = 0
                    return dividend # there is not dividend today, just return here

            if div_ccy == self.fund_ccy_:
                div_fx = 1
            else:
                fx_tbl = 'prices'
                # from_date_str = ex_date.strftime(g_opsdb_dateformat) # 2026-12-01
                from_date_str = self.expected_nav_date_.strftime(g_opsdb_dateformat) # 2026-12-01, cannot use ex_date to find the FX rate, should follow the expected NAV date
                sql_fx = (f"select value from {fx_tbl} where from_instrument = '{div_ccy}' and to_instrument = '{self.fund_ccy_}' "
                    f"and from_date = '{from_date_str}' and status='ACTIVE' and is_latest = 1")

                with opsDbClnt.cursor() as cursor:
                    cursor.execute(sql_fx)
                    rows = cursor.fetchall()
                    if len(rows) == 0:
                        msg = f"No FX rate found for {div_ccy} to {self.fund_ccy_} on {from_date_str}."
                        logger.error(msg)
                        raise ValueError(msg)
                    elif len(rows) > 1:
                        msg = f"Multiple FX rates found for {div_ccy} to {self.fund_ccy_} on {from_date_str}. Unexpected."
                        logger.error(msg)
                        raise ValueError(msg)
                    else:
                        div_fx = float(rows[0][0])
                
            dividend_fund_ccy = dividend * div_fx
            return dividend_fund_ccy

        # in t_fund_statics, there is MIC for fund, consider get from t_fund_statics
    def _getMicCode(self, tradar_code: str) -> str:
        mic_code = ""
        if tradar_code in ["CO-CHST", "HK-HSI"]:
            mic_code = "XHKG"
        elif tradar_code in ["HK-CKTH"]:
            mic_code = "KORX"
        return mic_code
    
    def _getIndexCodeSrc(self, tradar_code: str):
        index_code = ""
        index_source = ""
        if tradar_code == "CO-CHST":
            index_code = "HSTECH"
            index_source = "HSTECH"
        elif tradar_code == "HK-HSI":
            index_code = "HSI"
            index_source = "HSI"
        elif tradar_code == "HK-CKTH":
            index_code = "KRTECH"
            index_source = "FTSE"

        return index_source, index_code

    def _RICfromTicker(self, ticker_code: str, region_code: str) -> str:
        if region_code == 'KP':
            ric = ".".join([ticker_code, 'KS'])
        elif region_code == 'TT':
            ric = ".".join([ticker_code, 'TW'])
        elif region_code == 'JT':
            ric = ".".join([ticker_code, 'T'])
        elif region_code == 'HK':
            ric = ".".join([ticker_code, 'HK'])
        elif region_code == 'US':
            ric = ".".join([ticker_code, 'US']) # not correct, pending
        elif region_code == 'C2':
            ric = ".".join([ticker_code, 'SZ'])
        else:
            ric = ticker_code
            msg = f"Unknown region code '{region_code}' for ticker '{ticker_code}'."
            logger.error(msg)
            raise ValueError(msg) # will result a Teams message

        return ric