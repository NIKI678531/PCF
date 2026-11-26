import logging
import os
import sys
import copy
from datetime import datetime, timedelta
from enum import Enum
import pymssql
import pandas as pd

from FtseIndexFileReader import FtseIndexFileReader
from IndexFileReader import IndexFileReader
from FtseDividendReader import FtseDividendReader
from DividendFileReader import DividendFileReader
from equityandindex_pcf import EquityAndIndex
from global_definition import g_lstFunds_am, g_lstFunds_pm

from pcf import Pcf, PCF_STATUS, g_db_dateformat, g_riskdb_dateformat, g_opsdb_dateformat
from lni_pcf import LnIPcf
from mmf_pcf import MmfPcf
from cc_pcf import CoverCallPcf
from fixedIncome_pcf import FixedIncomePcf
from equity_pcf import EquityPcf
from equitybyindex_pcf import EquityByIndexPcf
from comdty_pcf import ComdtyPcf


from db_connection import ENV

logger = logging.getLogger('Main')

class PcfMaster:
    def __init__(self, funds: list[str], batch: str, produce_date, riskDbClnt, opsDbClnt, pcfGenDbClnt, teams_msger, env, pcf_file_date=None):
        self.lstTradarCode_ = funds
        self.pcf_produce_date_ = datetime.strptime(produce_date, "%Y%m%d")

        self.batch_ = batch
        
        self.riskDbClnt_ = riskDbClnt
        self.opsDbClnt_ = opsDbClnt
        self.pcfGenDbClnt_ = pcfGenDbClnt
        self.teams_msger_ = teams_msger
        self.env_ = env

        self.fundid_map_ = None # only map tradarcode to master class CdbFundId
        self._MapFundId() # DataFrame with columns: ["TradarCode", "CdbFundId"]

        self.pcf_path_ = r'\\csop.local\dfsroot\project\Public Folder\ETFoperations\pcfgen_uat'
        # self.pcf_path_ = r'c:\temp\t8' # 

        self.pcf_release_path_ = r'\\csop.local\dfsroot\project\Public Folder\ETFoperations\PCF_Production'
        # self.pcf_release_path_ = r'c:\temp\t1' # 

        self.lstCoverCallFunds_ = ['HK-HCECC', 'HK-CKCC']
        self.lstMmfFunds_ = ['CO-USTB', 'CO-MMF', 'CO-CUSF']
        self.lstFixedIncomeFunds_ = ['HK-UST20', 'SG-WGBI', 'SG-WGBI_X']
        self.lstEquityFunds_ = ['HK-CSAI', 'HK-CPTC', 'HK-CSI5'] # equity pcf using holding
        self.lstEquityIndexFunds_ = ['CO-CHST', 'HK-HSI'] # equity pcf using index
        self.lstEquityAndIndexFunds_ = ['HK-CKTH'] # equity and index pcf, 
        self.lstComdtyFunds_ = ['HK-CGDF']

        # list of index readers
        self.hstech_index_reader_ = IndexFileReader(self.pcfGenDbClnt_, "t_indx_weight")
        self.hsi_index_reader_ = IndexFileReader(self.pcfGenDbClnt_, "t_indx_weight")
        self.ftse_index_reader_ = FtseIndexFileReader(self.pcfGenDbClnt_, "t_indx_weight")
        self.index_file_path_ = r'\\csop.local\dfsroot\project\Public Folder\ETFoperations\IndexFile' # will append index code to form the final path

        # list of dividend readers
        self.hstech_dividend_reader_ = DividendFileReader(self.pcfGenDbClnt_, "t_indx_dividend")
        self.hsi_dividend_reader_ = DividendFileReader(self.pcfGenDbClnt_, "t_indx_dividend")
        self.ftse_dividend_reader_ = FtseDividendReader(self.pcfGenDbClnt_, "t_indx_dividend")

        self.dividend_file_path_ = r'\\csop.local\dfsroot\project\Public Folder\ETFoperations\IndexFile' # will append index code to form the final path

        self.lstPcfs_ = self._InitPcfs(pcf_file_date) # list of Pcf objects
        self.lstPendingPcf_ = []

    def _InitPcfs(self, pcf_file_date):
        lstPcfs = []
        for tradar_code in self.lstTradarCode_:
            if pcf_file_date is None:
                if self.batch_ == 'PM':
                    pcf_date_ymd = self._getPmPcfDate(tradar_code).strftime('%Y%m%d') # return pcf file date
                elif self.batch_ == 'AM':
                    pcf_date_ymd = self._getAmPcfDate(tradar_code).strftime('%Y%m%d')
                else:
                    raise ValueError(f"PcfMaster._InitPcfs: Unknown batch type: {self.batch_}")
            else:
                pcf_date_ymd = pcf_file_date # this is manual mode, using provided pcf file date

            cdb_fund_id = self.fundid_map_.loc[self.fundid_map_["TradarCode"] == tradar_code, "CdbFundId"].values[0]
            if tradar_code in self.lstCoverCallFunds_:
                pcf = CoverCallPcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)
            elif tradar_code in self.lstMmfFunds_:
                pcf = MmfPcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)
            elif tradar_code in self.lstFixedIncomeFunds_:
                # for SG-WGBI_X and SG-WGBI, they are two pcf objects with same cdb_fund_id, different tradar_code, e.g. SG-WGBI and SG-WGBI_X
                pcf = FixedIncomePcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)
            elif tradar_code in self.lstEquityFunds_:
                pcf = EquityPcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)
            elif tradar_code in self.lstEquityIndexFunds_:
                pcf = EquityByIndexPcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)
                # for Index Equity Fund, need to read the index file first.
                expected_nav_date = pcf.getExpectedNavDate()
                expected_nav_date_str = expected_nav_date.strftime('%Y%m%d')
                if tradar_code == "CO-CHST":
                    index_source = "HSTECH"
                    index_folder = "HSTECH"
                    index_reader = self.hstech_index_reader_
                    dividend_reader = self.hstech_dividend_reader_
                    index_file = f"{index_source}_opn_con_{expected_nav_date_str}.csv"
                    dividend_file = f"{index_source}_div_{expected_nav_date_str}.csv"
                elif tradar_code == "HK-HSI":
                    index_source = "HSI"
                    index_folder = "HSI"
                    index_reader = self.hsi_index_reader_
                    dividend_reader = self.hsi_dividend_reader_
                    index_file = f"{index_source}_opn_con_{expected_nav_date_str}.csv"
                    dividend_file = f"{index_source}_div_{expected_nav_date_str}.csv"
                else:
                    raise ValueError(f"{tradar_code} index file not handled")
                
                index_file_path = os.path.join(self.index_file_path_, index_folder)
                index_file_full_path = os.path.join(index_file_path, index_file)

                logger.info(f"Reading index file {index_source}, {index_file_full_path}")
                index_reader.ReadFile(index_file_full_path, index_source, pcf_date_ymd)

                if tradar_code in ["CO-CHST", "HK-HSI"]:
                    mic = "XHKG"
                    dividend_file_path = os.path.join(self.dividend_file_path_, index_folder)
                    div_file_full_path = os.path.join(dividend_file_path, dividend_file)
                    
                    dividend_reader.ReadFile(div_file_full_path, mic, index_source)

            elif tradar_code in self.lstEquityAndIndexFunds_:
                pcf = EquityAndIndex(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)
                if tradar_code == "HK-CKTH": # Equity + Index Hybrid
                    index_source = "FTSE"
                    index_folder = "FTSEHKKRTech"
                    index_reader = self.ftse_index_reader_
                    dividend_reader = self.ftse_dividend_reader_
                    pcf_date_dm = pcf_date_ymd[6:8] + pcf_date_ymd[4:6] # e.g. 20261116 -> 1611
                    index_file = f"ftkrtpo{pcf_date_dm}.csv"
                    hk_div_file = f"ftsctpnt{pcf_date_dm}.csv"
                    kr_div_file = f"ftktphnt{pcf_date_dm}.csv"

                index_file_path = os.path.join(self.index_file_path_, index_folder)
                index_file_full_path = os.path.join(index_file_path, index_file)

                logger.info(f"Reading index file {index_source}, {index_file_full_path}")
                index_reader.ReadFile(index_file_full_path, index_source, pcf_date_ymd)

                if tradar_code == "HK-CKTH": # read two index dividend files
                    dividend_file_path = os.path.join(self.dividend_file_path_, index_folder)
                    hk_div_file_full_path = os.path.join(dividend_file_path, hk_div_file)
                    kr_div_file_full_path = os.path.join(dividend_file_path, kr_div_file)
                                                        
                    dividend_reader.ReadFile(hk_div_file_full_path, "XHKG", index_source)
                    dividend_reader.ReadFile(kr_div_file_full_path, "XKOR", index_source)

            elif tradar_code in self.lstComdtyFunds_:
                pcf = ComdtyPcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)
            else:
                pcf = LnIPcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)

            lstPcfs.append(pcf)

        return lstPcfs

    def _MapFundId(self) -> None:
        rows = []
        with self.opsDbClnt_.cursor() as cursor:
            for tradar_code in self.lstTradarCode_:
                # if tradar_code end with "_X", it is a listed share class. For now, only case is SG-WGBI_X, skip it, because will get CdbFundId for SG-WGBI 
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
                    continue # continue with next fund
                fid = fid_row[0]

                cursor.execute(
                    f"SELECT CdbFundId FROM t_FDE_Cdb_FundMap WHERE Fid = {fid}"
                )
                cdb_row = cursor.fetchone()
                if cdb_row is None:
                    logger.critical(f"No CdbFundId found for Fid {fid} (TradarCode {tradar_code}). Exiting.")
                    sys.exit(1)

                rows.append({"TradarCode": tradar_code, "CdbFundId": cdb_row[0]})

        self.fundid_map_ = pd.DataFrame(rows, columns=["TradarCode", "CdbFundId"])

        # logger.info(f"FundId mapping completed.\n {self.fundid_map_}")

    def _getAmPcfDate(self, tradar_code) -> datetime:
        return self.pcf_produce_date_
    
    def _getPmPcfDate(self, tradar_code) -> datetime:
        sql = f"select TradarCode, HolidayDate from t_pcf_holiday where TradarCode = '{tradar_code}' order by HolidayDate asc"
        with self.pcfGenDbClnt_.cursor() as cursor:
            cursor.execute(sql)
            holiday_rows = cursor.fetchall()

        lst_holiday_dates = [str(row[1]) for row in holiday_rows]

        current_date = self.pcf_produce_date_

        # pcf_date is T+1, if T+1 is saturday or sunday or holiday, then pcf_date is T+2, T+3, etc.
        pcf_date_dt = (current_date + timedelta(days=1))
        while True:
            if pcf_date_dt.weekday() >= 5:  # Saturday or Sunday
                pcf_date_dt += timedelta(days=1)
                continue

            is_holiday = pcf_date_dt.strftime(g_db_dateformat) in lst_holiday_dates
            if is_holiday:
                pcf_date_dt += timedelta(days=1)
                continue

            break

        return pcf_date_dt

    def GetPcfProduceDate(self):
        return self.pcf_produce_date_.strftime(g_db_dateformat)

    def GeneratePcf4Fund(self, tradar_code, filedate):
        generated = False
        pcf_date_ymd = filedate

        cdb_fund_id = self.fundid_map_.loc[self.fundid_map_["TradarCode"] == tradar_code, "CdbFundId"].values[0]
        
        if tradar_code in self.lstCoverCallFunds_:
            pcf = CoverCallPcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)
        elif tradar_code in self.lstMmfFunds_:
            pcf = MmfPcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)
        elif tradar_code in self.lstFixedIncomeFunds_:
            # for SG-WGBI_X and SG-WGBI, they are two pcf objects with same cdb_fund_id, different tradar_code, e.g. SG-WGBI and SG-WGBI_X
            pcf = FixedIncomePcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)
        elif tradar_code in self.lstEquityFunds_:
            pcf = EquityPcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)
        elif tradar_code in self.lstEquityIndexFunds_:
            pcf = EquityByIndexPcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)
            # for Index Equity Fund, need to read the index file first.
            expected_nav_date = pcf.getExpectedNavDate()
            expected_nav_date_str = expected_nav_date.strftime('%Y%m%d')
            if tradar_code == "CO-CHST":
                index_source = "HSTECH"
                index_folder = "HSTECH"
                index_reader = self.hstech_index_reader_
                dividend_reader = self.hstech_dividend_reader_
                index_file = f"{index_source}_opn_con_{expected_nav_date_str}.csv"
                dividend_file = f"{index_source}_div_{expected_nav_date_str}.csv"
            elif tradar_code == "HK-HSI":
                index_source = "HSI"
                index_folder = "HSI"
                index_reader = self.hsi_index_reader_
                dividend_reader = self.hsi_dividend_reader_
                index_file = f"{index_source}_opn_con_{expected_nav_date_str}.csv"
                dividend_file = f"{index_source}_div_{expected_nav_date_str}.csv"
            elif tradar_code == "HK-CKTH":
                index_source = "FTSE"
                index_folder = "FTSEHKKRTech"
                index_reader = self.ftse_index_reader_
                dividend_reader = self.ftse_dividend_reader_
                pcf_date_dm = pcf_date_ymd[6:8] + pcf_date_ymd[4:6] # e.g. 20261116 -> 1611
                index_file = f"ftkrtpo{pcf_date_dm}.csv"
                hk_div_file = f"ftsctpnt{pcf_date_dm}.csv"
                kr_div_file = f"ftktphnt{pcf_date_dm}.csv"
            else:
                raise ValueError(f"{tradar_code} index file not handled")
            
            index_file_path = os.path.join(self.index_file_path_, index_folder)
            index_file_full_path = os.path.join(index_file_path, index_file)

            logger.info(f"Reading index file {index_source}, {index_file_full_path}")
            index_reader.ReadFile(index_file_full_path, index_source, pcf_date_ymd)

            if tradar_code in ["CO-CHST", "HK-HSI"]:
                mic = "XHKG"
                dividend_file_path = os.path.join(self.dividend_file_path_, index_folder)
                div_file_full_path = os.path.join(dividend_file_path, dividend_file)
                
                dividend_reader.ReadFile(div_file_full_path, mic, index_source)
            elif tradar_code in ["HK-CKTH"]:
                dividend_file_path = os.path.join(self.dividend_file_path_, index_folder)
                hk_div_file_full_path = os.path.join(dividend_file_path, hk_div_file)
                kr_div_file_full_path = os.path.join(dividend_file_path, kr_div_file)
                                    
                dividend_reader.ReadFile(hk_div_file_full_path, "XHKG", index_source)
                dividend_reader.ReadFile(kr_div_file_full_path, "XKOR", index_source)

        elif tradar_code in self.lstComdtyFunds_:
            pcf = ComdtyPcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)
        else:
            pcf = LnIPcf(tradar_code, cdb_fund_id, pcf_date_ymd, self.pcfGenDbClnt_, self.opsDbClnt_)

        if tradar_code == "HK-GLD-L":
            path_tradar_code = "HK-GOLD-L"
        elif tradar_code == "SG-WGBI_X":
            path_tradar_code = "SG-WGBIX"
        else:
            path_tradar_code = tradar_code

        pcf_path = os.path.join(self.pcf_path_, path_tradar_code) + os.sep
        os.makedirs(pcf_path, exist_ok=True)

        pcf_release_path = self.pcf_release_path_ + os.sep

        release_pcf = False

        if self.env_ == ENV.PROD:
            if tradar_code in g_lstFunds_am:
                release_pcf = True
            else:
                if tradar_code in ["HK-CKCC", "HK-KIOX-125L"]:
                    release_pcf = True
                else:
                    release_pcf = False
        else:
            release_pcf = False # for any environment other than PROD, set release_pcf to False
    
        status = pcf.GeneratePcf(self.riskDbClnt_, self.opsDbClnt_, self.pcfGenDbClnt_, pcf_path, pcf_release_path, release=release_pcf, force=True)

        if status in (PCF_STATUS.Done, PCF_STATUS.Revise):
            title = f"PCF Status Update - ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
            msg = f"Manually generate PCF for {tradar_code} as of {filedate}"
            logger.info(f"TeamsMessage: {msg}")
            self.teams_msger_.sendMessage(msg, title)
            generated = True
        else:
            title = f"PCF Status Update - ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
            msg = f"Manually generate PCF for {tradar_code} as of {filedate} failed"
            logger.info(f"TeamsMessage: {msg}")
            self.teams_msger_.sendMessage(msg, title)
            generated = False
        
        if not generated:
            logger.error(f"{tradar_code} manual generate PCF failed")
        
        return generated
        
            
    def _PendingPcfChanged(self, lstPending) -> bool:
        # Compare lstPending with self.lstPendingPcf_, return true if any difference, otherwise return false
        if len(lstPending) != len(self.lstPendingPcf_):
            return True
        
        for item in lstPending:
            if item not in self.lstPendingPcf_:
                return True
        
        for item in self.lstPendingPcf_:
            if item not in lstPending:
                return True
            
        return False

    # we only want to send pending message when pending funds are changed. But if the change is changed to empty list, we don't want to send message.
    def _tracePendingPcf(self, lstPending):
        if self._PendingPcfChanged(lstPending) and len(lstPending) > 0:
            self.lstPendingPcf_ = copy.deepcopy(lstPending)
            return True
        else:
            return False

    def _missingTraderCode(self, lst_tradar_code) -> list:
        # check if tradar_code is in self.lstTradarCode_, return a list of missing tradar_codes
        missing_tradar_codes = []
        for pcf in self.lstPcfs_:
            if pcf.tradar_code_ not in lst_tradar_code:
                missing_tradar_codes.append(pcf.tradar_code_)
        return missing_tradar_codes
        

    def GenerateAllPcf(self):
        tradar_code_Done = []
        tradar_code_Revise = []
        tradar_code_pending = []
        # tradar_code_failed = []

        logger.info(f"GenerateAllPcf start...")

        for pcf in self.lstPcfs_:
            # keep try block inside the loop to make sure one fund's failure will not impact other funds' generation.
            try:
                tradar_code = pcf.tradar_code_
                product_date_ymd = self.pcf_produce_date_.strftime(g_db_dateformat)
                if pcf.IsPcfHoliday(product_date_ymd):
                    logger.info(f"GenerateAllPcf: {tradar_code} is PCF holiday on {product_date_ymd}")
                    continue

                if tradar_code == "HK-GLD-L":
                    path_tradar_code = "HK-GOLD-L"
                elif tradar_code == "SG-WGBI_X":
                    path_tradar_code = "SG-WGBIX" # based on current system, but I think SG-WGBI_X should be better, can not use ending with '_X', cannot use ending with 'X', may have ready tradar code end with 'X', e.g. NDX
                else:
                    path_tradar_code = tradar_code

                pcf_path = os.path.join(self.pcf_path_, path_tradar_code) + os.sep
                os.makedirs(pcf_path, exist_ok=True)

                pcf_release_path = self.pcf_release_path_ + os.sep
                # os.makedirs(pcf_release_path, exist_ok=True)

                release_pcf = False

                if self.env_ == ENV.PROD:
                    if self.batch_ == "AM":
                        release_pcf = True
                    elif tradar_code in ["HK-CKCC", "HK-KIOX-125L"]:
                        release_pcf = True
                else:
                    release_pcf = False # for any environment other than PROD, set release_pcf to False
                    
                pcf_status = pcf.GeneratePcf(self.riskDbClnt_, self.opsDbClnt_, self.pcfGenDbClnt_, pcf_path, pcf_release_path, release=release_pcf)

                pcf_file_date_dt = datetime.strptime(pcf.pcf_file_date_, g_db_dateformat)
                pcf_date_str = pcf_file_date_dt.strftime("%Y-%m-%d")
                
                if pcf_status == PCF_STATUS.Done:
                    tradar_code_Done.append({
                        "tradar_code": tradar_code,
                        "expected_nav_date": pcf.expected_nav_date_.strftime("%Y-%m-%d"),
                        "pcf_date": pcf_date_str
                    })
                elif pcf_status == PCF_STATUS.Revise:
                    tradar_code_Revise.append(tradar_code)
                elif pcf_status == PCF_STATUS.Pending:
                    tradar_code_pending.append(tradar_code)
                    
            except Exception as e:
                logger.exception(f"Error during PCF generation: {e}")
                tradar_code_pending.append(tradar_code)  # Treat as pending if failed

        title = f"{self.batch_} PCF Status Update - ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
        msg = ""
        if tradar_code_Done:
            msg += "PCF just done:\n"
            msg += "Tradar Code, Expected NAV Date, PCF Date\n"
            for done_pcf in tradar_code_Done:
                msg += f"{done_pcf['tradar_code']}, {done_pcf['expected_nav_date']}, {done_pcf['pcf_date']}\n"

            done_tradar_codes = [done_pcf["tradar_code"] for done_pcf in tradar_code_Done]
            pending_pcfs = self._missingTraderCode(done_tradar_codes)
            if len(pending_pcfs) == 0:
                msg += "All done.\n"

        if tradar_code_Revise:
            msg += "PCF just revise: " + ",".join(tradar_code_Revise) + "\n"

        if self._tracePendingPcf(tradar_code_pending):
                # the pending code changed compare last time send message. this is to avoid keep on sending pending funds
                msg += "PCF pending: " + ",".join(tradar_code_pending) + "\n"
        
        if len(msg) > 0:
            msg = msg[:-1] # remove the last newline
            logger.info(f"TeamsMessage: {msg}")
            self.teams_msger_.sendMessage(msg, title)

        logger.info(f"GenerateAllPcf completed.")
