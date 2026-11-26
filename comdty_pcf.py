from datetime import datetime, timedelta

import pandas as pd
from pcf import Pcf, g_db_dateformat, g_riskdb_dateformat, g_opsdb_dateformat, PcfType, PCF_STATUS


import logging

logger = logging.getLogger('Main')

class ComdtyPcf(Pcf):
    CLASSIFICATION_INVESTMENTS = "Investments"
    RECON_TYPE = "NAV1"
    ROUND_LOT = 32.148

    def __init__(self, tradar_code: str, cdb_fund_id: str, pcf_date: str, pcfGenDbClnt, opsDbClnt):
        super().__init__(tradar_code, cdb_fund_id, pcf_date, pcfGenDbClnt, opsDbClnt)
        
        self.holding_classification_ = [self.CLASSIFICATION_INVESTMENTS] # use to filter the components in PCF file
        self.holdings_ = None

    def _MergeHolding(self, holding_df: pd.DataFrame) -> pd.DataFrame:
        # For rows with the same Ticker: SUM numeric position fields, keep "first item" for other columns.
        # Build the agg spec dynamically so adding/removing columns upstream doesn't break this.

        merged_df = holding_df.sort_values(by="Ticker", kind="stable").reset_index(drop=True)
        drop_indexes = []

        previous_index = 0

        for current_index in range(1, len(merged_df)):
            if merged_df.at[current_index, "Ticker"] == merged_df.at[previous_index, "Ticker"]:
                if "Amount" in merged_df.columns:
                    merged_df.at[previous_index, "Amount"] += merged_df.at[current_index, "Amount"]

                if "Exposure_NAV_Percent" in merged_df.columns:
                    merged_df.at[previous_index, "Exposure_NAV_Percent"] += merged_df.at[current_index, "Exposure_NAV_Percent"]

                drop_indexes.append(current_index)
            else:
                previous_index = current_index

        merged_df = merged_df.drop(index=drop_indexes).reset_index(drop=True)

        return merged_df[holding_df.columns]
    
    def _getHoldingIdentity(self, ticker: str):
        # find the "Ticker", "ISIN" from self.holdings_
        row = self.holdings_.loc[self.holdings_["Ticker"] == ticker]

        if not row.empty:
            bbgticker = ticker
            # generate ric from ticker, e.g. ticker = 'XAUUSD CURNCY', ric = 'XAUUSD CURNCY'
            ticker_parts = ticker.split()
            if len(ticker_parts) >= 2:
                ticker_code = ticker_parts[0].zfill(4) if len(ticker_parts[0]) < 4 else ticker_parts[0]
                yellow_key = ticker_parts[1]
                if yellow_key == 'Curncy':
                    ric = " ".join([ticker_code, 'Curncy'])
                else:
                    ric = ticker
                    msg = f"Unknown yellow key '{yellow_key}' for ticker '{ticker}'."
                    logger.error(msg)
                    raise ValueError(msg) # will result a Teams message
            else:
                ric = ticker

            isin = row["ISIN"].values[0]
        else:
            bbgticker = None
            ric = None
            isin = None

            logger.error(f"_getHoldingIdentity: missing {ticker} self.holdings_.")

        return bbgticker, ric, isin
    
    def _SyncPcfComponents(self, riskDbClnt, opsDbClnt, pcfGenDbClnt, as_of_date: datetime) -> bool:
        
        with riskDbClnt.cursor() as cursor:
            holdingid_sql = f"select acct_holding_id from account_holdings where acct_book_id = '{self.cdb_fund_id_}' and as_of_date = '{as_of_date.strftime(g_riskdb_dateformat)}' and holding_type = '{self.RECON_TYPE}' and is_latest = 1"
            cursor.execute(holdingid_sql)
            row = cursor.fetchone()
            if row is None:
                logger.info(f"Sql: {holdingid_sql}")
                logger.error(f"No {self.RECON_TYPE} holding for fund {self.tradar_code_} as of date {as_of_date.strftime(g_db_dateformat)}.")
                return False
            
            acct_holding_id = row[0]

            classification_str = ''
            for classification in self.holding_classification_:
                classification_str += f"'{classification}',"
            classification_str = classification_str[:-1] # remove the last comma

            # for each fund, sql_holding may need to add more conditions from fund to fund.
            sql_holding = (
                f"select ticker, isin, amount, price, ccy, fx_lc_fc, security_desc, fund_ccy, classification, val_point from account_holdings_data "
                f"where acct_holding_id = '{acct_holding_id}' and classification in ({classification_str})"
            )
            cursor.execute(sql_holding)
            rows = cursor.fetchall()

            # save the result in a pandas dataframe for later use in PCF generation.
            holdings = pd.DataFrame(rows, columns=['Ticker', 'ISIN', 'Amount', 'Price', 'CCY', 'FX_LC_FC', 'Security_Desc', 'Fund_CCY', 'Classification', 'Val_Point'])
            holdings[['Amount', 'Price', 'FX_LC_FC', 'Val_Point']] = holdings[['Amount', 'Price', 'FX_LC_FC', 'Val_Point']].astype(float)
            # ticker = "SOBRKB2L Index"

            if holdings.empty:
                logger.info(f"Sql: {sql_holding}")
                logger.error(f"No holding with classification '{self.holding_classification_}' for fund {self.tradar_code_} as of date {as_of_date.strftime(g_db_dateformat)}.")
                return False
            
            self.holdings_ = self._MergeHolding(holdings)

            logger.info(f"{self.tradar_code_} holding for PCF generation (e.g. after merging)\n%s", self.holdings_.to_csv(sep="\t", index=False, lineterminator="\n").rstrip())

            self.fund_ccy_ = self.holdings_["Fund_CCY"].iloc[0] if not self.holdings_.empty else None # fund_ccy should be same for all rows.

        with opsDbClnt.cursor() as cursor:
            sql_total_fund_units= (
                f"select pma_units from fund_level_data where acct_book_id = '{self.cdb_fund_id_}' and acct_book_class_id = '{self.cdb_listed_class_id_}' "
                f"and recon_type = '{self.RECON_TYPE}' and as_of_date = '{as_of_date.strftime(g_opsdb_dateformat)}'"
            )
            cursor.execute(sql_total_fund_units)
            row = cursor.fetchone()
            if row is None:
                logger.info(f"Sql: {sql_total_fund_units}")
                logger.error(f"No fund level data found for fund {self.tradar_code_} {self.cdb_fund_id_} as of date {as_of_date.strftime(g_db_dateformat)}.")
                return False
            
            list_shares = float(row[0])

        # self.holdings_["Contract_Size"] = 1.0

        # from pcfGenDbClnt table t_ticker_mic get the MIC for each ticker from self.holdings_. If not found, MIC = 'XXXX'.
        self.holdings_["MIC"] = 'XXXX'
        with pcfGenDbClnt.cursor() as cursor:
            for ticker in self.holdings_["Ticker"]:
                cursor.execute(f"select MIC from t_ticker_mic where ticker = '{ticker}'")
                row = cursor.fetchone() # ticker is the primary key, so only one row is expected.
                if row is not None:
                    self.holdings_.loc[self.holdings_["Ticker"] == ticker, "MIC"] = row[0]

        # from pcfGenDbClnt table t_ticker_bbgric get the Bloomberg ticker for each ticker from self.holdings_. If not found, bbgticker = 'XXXX'.
        self.holdings_["BbgTicker"] = ''
        self.holdings_["RIC"] = ''
        self.holdings_["Isin"] = '' # when write PCF, we use this column
        with pcfGenDbClnt.cursor() as cursor:
            for ticker in self.holdings_["Ticker"]:
                # get the Classification from self.holdings_ for this ticker
                classification = self.holdings_.loc[self.holdings_["Ticker"] == ticker, "Classification"].values[0]
                if classification == self.CLASSIFICATION_INVESTMENTS:
                    # for Investments, all are stocks, we find the data from holding data, not in the db table
                    bbgticker, ric, isin = self._getHoldingIdentity(ticker)
                    self.holdings_.loc[self.holdings_["Ticker"] == ticker, "BbgTicker"] = bbgticker
                    self.holdings_.loc[self.holdings_["Ticker"] == ticker, "RIC"] = ric
                    self.holdings_.loc[self.holdings_["Ticker"] == ticker, "Isin"] = isin
        
        as_of_date_str = as_of_date.strftime(g_db_dateformat)

        listShareWeight = self.ComponentListWeight(riskDbClnt, opsDbClnt, as_of_date.strftime(g_opsdb_dateformat)) # this is the weight of the listed share class in the fund, used to calculate the amount of each component in the listed share class.
        
        logger.info(f"SyncPcfComponents: {self.tradar_code_}, {as_of_date_str}, list_shares={list_shares}, listShareWeight={listShareWeight}, basket_size={self.basket_size_}")
        
        # Calculate shares_per_basket for each ticker. shares_per_basket = "Shares" column in the PCF
        for holding_index, holding_row in self.holdings_.iterrows():
            list_weighted_shares = holding_row['Amount'] * listShareWeight
            list_weighted_shares_per_basket =  (list_weighted_shares / (list_shares / self.basket_size_))
            list_weighted_shares_per_basket_rounded = round(list_weighted_shares_per_basket/self.ROUND_LOT, 0)*self.ROUND_LOT
            self.holdings_.at[holding_index, 'Iopv_Shres_Per_Basket'] = list_weighted_shares_per_basket

            logger.info(f"Holding {holding_row['Ticker']}, Shares: {list_weighted_shares}, Shares Per Basket rounded (PCF): {list_weighted_shares_per_basket_rounded}, Shares Per Basket unrounded (iopv): {list_weighted_shares_per_basket}")

            # for Commodity, the rounding is done to the nearest ROUND_LOT
            if holding_row["Classification"] == self.CLASSIFICATION_INVESTMENTS:
                # truncated_shares = int(list_weighted_shares_per_basket)
                # rounded_shares = truncated_shares + (1 if (list_weighted_shares_per_basket - truncated_shares) > 0.5 else 0)

                self.holdings_.at[holding_index, 'Shares_Per_Basket'] = list_weighted_shares_per_basket_rounded
                self.holdings_.at[holding_index, 'Iopv_Shres_Per_Basket'] = list_weighted_shares_per_basket
            else: # this else looks dummy, can compare with Equity PCF
                self.holdings_.at[holding_index, 'Shares_Per_Basket'] = list_weighted_shares_per_basket_rounded
                self.holdings_.at[holding_index, 'Iopv_Shres_Per_Basket'] = list_weighted_shares_per_basket

        logger.info(f"{self.tradar_code_} holding\n{self.holdings_}")

        try:
            # before insert, delete existing records for the same TradarCode and AsOfDate to avoid duplicate records.
            for table in ["t_pcf_holding", "t_iopvpcf_holding"]:
                  with pcfGenDbClnt.cursor() as cursor:
                    cursor.execute(f"DELETE FROM {table} WHERE TradarCode = '{self.tradar_code_}' AND AsOfDate = '{as_of_date_str}'")

                    for index, row in self.holdings_.iterrows():
                        ticker = row["Ticker"]

                        if table == "t_pcf_holding":
                            shares_per_basket = row['Shares_Per_Basket'] # [(amt of this component in this fund)/total shares]*basket_size = shares per basket
                        elif table == "t_iopvpcf_holding":
                            shares_per_basket = row['Iopv_Shres_Per_Basket']

                        logger.info(f"SyncPcfComponents: table={table}, tradar_code={self.tradar_code_}, ticker={ticker}, share_per_basket={shares_per_basket}, basket_size={self.basket_size_}")

                        cursor.execute(f"""
                            INSERT INTO {table} (TradarCode, Ticker, Share, Price, Ccy, FxRate, Description, AsOfDate, Coupon, ValPoint)
                            VALUES ('{self.tradar_code_}', '{ticker}', {shares_per_basket}, {row["Price"]}, '{row["CCY"]}', {row["FX_LC_FC"]}, '{row["Security_Desc"]}', '{as_of_date_str}', 0, {row['Val_Point']})
                        """)
            pcfGenDbClnt.commit()
        except Exception as e:
            logger.exception(f"SyncPcfComponents persist {table} for fund {self.tradar_code_} as of date {as_of_date_str}: {e}")
            return False
        
        return True

    def _CalcShares(self, total_units: float) -> float:
        # for lnI, calcshares = total_units. But for other fund, it can be different. So put the logic in different function for easy override.
        return total_units

    def _CalcComponentValue(self, table):
        component_value = 0.0
        for index, row in self.holdings_.iterrows():
            if table == "t_pcf_fund":
                component_share_per_basket = row['Shares_Per_Basket'] # for pcf, the Shares_Per_Basket is rounded
            elif table == "t_iopvpcf_fund":
                component_share_per_basket = row['Iopv_Shres_Per_Basket'] # for iopvpcf, the Iopv_Shres_Per_Basket is unrounded

            component_price = round(row["Price"], 4)
            component_fx = row["FX_LC_FC"]
            component_contract_size = row['Val_Point']
            row_value = (component_share_per_basket * component_price * component_contract_size)/component_fx
            component_value += row_value

        component_value = round(component_value, 4)

        return component_value
    
    def _SyncPcfFundData(self, riskDbClnt, opsDbClnt, pcfGenDbClnt, as_of_date: datetime) -> bool:

        # Sync components first, then sync fund level data. Because fund level data need count of holdings.
        if self._SyncPcfComponents(riskDbClnt, opsDbClnt, pcfGenDbClnt, as_of_date):
            logger.info(f"SyncPcfComponents succeeded. Now sync Pcf fund level data.") 

            ops_table = "fund_level_data"

            sql_nav1 = f"""select as_of_date, recon_type, acct_book_id as fund_id, acct_book_class_id as class_id, 
                      pma_nav as nav, pma_nav_per_share as nav_per_share, pma_units as units from {ops_table}
                      where recon_type = 'NAV1' and as_of_date = '{as_of_date.strftime(g_riskdb_dateformat)}' and is_nav_holiday = 0 and acct_book_id = '{self.cdb_fund_id_}' and acct_book_class_id = '{self.cdb_listed_class_id_}'
                      """

            with opsDbClnt.cursor() as cursor:
                cursor.execute(sql_nav1)
                row = cursor.fetchone()
                if row is None:
                    logger.info(f"Sql: {sql_nav1}")
                    logger.error(f"No fund level data found for fund {self.tradar_code_} {self.cdb_fund_id_} as of date {as_of_date.strftime(g_db_dateformat)}.")
                    return False
                
                nav1 = float(row[4])
                nav1_per_share = float(row[5])
                units1 = float(row[6])

            sql2 = f"select BasketSize, FeeRate, FeeFixed from t_fund_statics where TradarCode = '{self.tradar_code_}'"
            with pcfGenDbClnt.cursor() as cursor:
                cursor.execute(sql2)
                row = cursor.fetchone()
                if row is None:
                    logger.info(f"Sql: {sql2}")
                    logger.error(f"No fund statics data found for fund {self.tradar_code_}.")
                    return False
                
                basket_size = int(row[0])
                # fee_rate = float(row[1])
                # fee_fixed = float(row[2])
            
            # calculate the estimated cash
            
            fundshares = self._CalcShares(units1) # cover call it is units2, but equity using units1 confirmed with stored-proc pro_make_orgdata

            # component_value = 0.0
            # for index, row in self.holdings_.iterrows():
            #     component_share_per_basket = row['Shares_Per_Basket'] # this is weighted for listed shared class
            #     component_price = round(row["Price"], 4)
            #     component_fx = row["FX_LC_FC"]
            #     component_contract_size = row['Val_Point']
            #     row_value = (component_share_per_basket * component_price * component_contract_size)/component_fx
            #     component_value += row_value
    
            # component_value = round(component_value, 4)

            # commodity same as equity using nav1_per_share
            # estimated_cash = round(nav1_per_share * basket_size - component_value, 4)

            # logger.info(f"calc estimated_cash {self.tradar_code_}: nav1pershare={nav1_per_share}, basket_size={basket_size}, component_value={component_value}, estimated_cash={estimated_cash}")
        else:
            logger.error(f"SyncPcfFundData: SyncPcfComponents failed. Sync pcf components then sync fund level data")
            return False
        
        as_of_date_str = as_of_date.strftime(g_db_dateformat)
        try:

            for table in ["t_pcf_fund", "t_iopvpcf_fund"]:

                component_value = self._CalcComponentValue(table)

                # commodity same as equity using nav1_per_share
                estimated_cash = round(nav1_per_share * basket_size - component_value, 4)
                if table == "t_pcf_fund":
                    table_key = 'pcf'
                else:
                    table_key = 'iopvpcf'

                logger.info(f"calc estimated_cash {self.tradar_code_} {table_key}: nav1pershare={nav1_per_share}, basket_size={basket_size}, component_value={component_value}, estimated_cash={estimated_cash}")

                with pcfGenDbClnt.cursor() as cursor:
                    # before insert, delete existing records for the same TradarCode and AsOfDate to avoid duplicate records.
                    cursor.execute(f"DELETE FROM {table} WHERE TradarCode = '{self.tradar_code_}' AND AsOfDate = '{as_of_date_str}'")

                    # note: here using NAV1 for fund level data
                    cursor.execute(f"""
                        INSERT INTO {table} (TradarCode, NavPerShare, EstimateCash, TotalShares, AsOfDate, FundCcy)
                        VALUES ('{self.tradar_code_}', {nav1_per_share}, {estimated_cash}, {fundshares}, '{as_of_date_str}', '{self.fund_ccy_}')
                    """)
                pcfGenDbClnt.commit()
        except Exception as e:
            logger.exception(f"Error inserting fund data into {table} for fund {self.tradar_code_} as of date {as_of_date_str}: {e}")
            return False
        
        logger.info(f"SyncPcfFundData done for {self.tradar_code_} {as_of_date_str}")
        return True

    def _WritePcfHeader(self, f_writer, filedate: str, iopvfile, pcfGenDbClnt, as_of_date_str) -> None:
        f_writer.write("," + self.fund_name_ + "\n")
        f_writer.write("\n")
        trade_date_dt = datetime.strptime(filedate, g_db_dateformat)
        trade_date = trade_date_dt.strftime("%d/%m/%Y")
        f_writer.write(",Indicative Creation / Redemption Basket Composition for Trade Date," + trade_date + "\n")
        f_writer.write("\n")
        f_writer.write(",FUND INFORMATION\n")
        f_writer.write(",Fund Name," + self.fund_name_ + "\n")
        f_writer.write(",ISIN," + self.isin_ + "\n")
        f_writer.write(",BBG Ticker," + self.bbgticker_ + "\n")
        f_writer.write(",Reuters Code," + self.reuterscode_ + "\n")
    
        # get fund level data from t_pcf_fund
        if iopvfile:
            table = "t_iopvpcf_fund"
        else:
            table = "t_pcf_fund"
            
        sql_fund = f"select NavPerShare, EstimateCash, TotalShares, FundCcy from {table} where TradarCode = '{self.tradar_code_}' and AsOfDate = '{as_of_date_str}'"
        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql_fund)
            row = cursor.fetchone()
            if row is None:
                logger.info(f"Sql: {sql_fund}")
                logger.error(f"WritePcfHeader: No fund data found for {self.tradar_code_} as of {filedate}")
                raise Exception(f"Cannot generate PCF for {self.tradar_code_} as of {filedate}")
            
            self.nav_per_share_ = row[0]
            self.total_shares_ = round(row[2], 0)
            
            if table == "t_iopvpcf_fund":
                self.iopv_estimated_cash_ = row[1]
                estimated_cash_raw = self.iopv_estimated_cash_ # self.iopv_estimated_cash_ is not used currently, but keep it for future use
            elif table == "t_pcf_fund":
                self.estimated_cash_ = row[1]
                estimated_cash_raw = self.estimated_cash_ # self.estimated_cash_ is used in writing hsbc file


        f_writer.write(",Fund Currency," + self.fund_ccy_ + "\n")
        nav_per_share = self._formatFloatAsInt(float(self.nav_per_share_)) # convert 1.0 to 1, but if 1.23, keep 1.23
        f_writer.write(",NAV per Share," + str(nav_per_share) + "\n")
        f_writer.write(",Number of Fund Shares in Issue," + str(self.total_shares_) + "\n")
        f_writer.write(",Total NAV of Fund per Creation / Redemption Basket" + "\n")
        f_writer.write(",Actual Cash per Creation / Redemption Basket" + "\n")

        estimated_cash = self._formatFloatAsInt(float(estimated_cash_raw))
        f_writer.write(",Estimated Cash per Creation / Redemption Basket," + str(estimated_cash) + "\n")
        f_writer.write(",Number of Fund Shares per Creation / Redemption Basket," + str(self.basket_size_) + "\n")
        f_writer.write("\n")

    # convert 1.0 to 1, but if 1.23, keep 1.23
    def _formatFloatAsInt(self, f_shares: float):
        shares = round(f_shares, 4)  
        if shares.is_integer():  
            shares = int(shares)  
        return shares
    
    # here we read from DB, because user may override the holding from UI.    
    def _WritePcf(self, filename: str, filedate: str, pcfGenDbClnt, as_of_date_str) -> None:
        with open(filename, "w") as f:
            self._WritePcfHeader(f, filedate, False, pcfGenDbClnt, as_of_date_str)

            f.write(",Company Name,Currency,Price,Shares,ISIN,SEDOL,RIC,Bloomberg Ticker\n")

            # get holding level data from t_pcf_holding
            sql_holding = f"select Ticker, Share, Price, Ccy, Description from t_pcf_holding where TradarCode = '{self.tradar_code_}' and AsOfDate = '{as_of_date_str}' order by Ticker"
            with pcfGenDbClnt.cursor() as cursor:
                cursor.execute(sql_holding)
                rows = cursor.fetchall()
                rows = sorted(rows, key=lambda row: "EQUITY" in str(row[0]).upper())

                index = 1
                for row in rows:
                    ticker = row[0]
                    share = self._formatFloatAsInt(float(row[1])) # convert 1.0 to 1, but if 1.23, keep 1.23
                    price = self._formatFloatAsInt(float(round(row[2],4))) # changed to 4 align with lni_pcf.py

                    ccy = row[3]
                    description = row[4]

                    isin = self.holdings_.loc[self.holdings_["Ticker"] == ticker, "Isin"].values[0]
                    if isin is None:
                        isin = '' # instead of write "none" in the pcf, write an empty string
                    ric = self.holdings_.loc[self.holdings_["Ticker"] == ticker, "RIC"].values[0]
                    bbgticker = self.holdings_.loc[self.holdings_["Ticker"] == ticker, "BbgTicker"].values[0]
                    
                    f.write(f"{index},{description},{ccy},{price},{share},{isin},,{ric},{bbgticker}\n")
                    index += 1

    def _WriteIopvPcf(self, filename: str, filedate: str, pcfGenDbClnt, as_of_date_str) -> None:
        with open(filename, "w") as f:
            self._WritePcfHeader(f, filedate, True, pcfGenDbClnt, as_of_date_str)

            f.write(",Company Name,Currency,Price,Shares,ISIN,SEDOL,RIC\n")
            # for debug
            # logger.info(f"holding: \n{self.holdings_}")

            # get holding level data from t_pcf_holding
            sql_holding = f"select Ticker, Share, Price, Ccy, Description from t_iopvpcf_holding where TradarCode = '{self.tradar_code_}' and AsOfDate = '{as_of_date_str}' order by Ticker"
            with pcfGenDbClnt.cursor() as cursor:
                cursor.execute(sql_holding)
                rows = cursor.fetchall()
                rows = sorted(rows, key=lambda row: "EQUITY" in str(row[0]).upper())

                index = 1
                for row in rows:
                    ticker = row[0]
                    share = self._formatFloatAsInt(float(row[1])) # convert 1.0 to 1, but if 1.23, keep 1.23
                    price = self._formatFloatAsInt(float(round(row[2],4))) # changed to 4 align with lni_pcf.py
                    ccy = row[3]
                    description = row[4]

                    isin = self.holdings_.loc[self.holdings_["Ticker"] == ticker, "Isin"].values[0]
                    if isin is None:
                        isin = '' # instead of write "none" in the pcf, write an empty string
                    ric = self.holdings_.loc[self.holdings_["Ticker"] == ticker, "RIC"].values[0]

                    f.write(f"{index},{description},{ccy},{price},{share},{isin},,{ric}\n") # Cover Call includes the contract size column for IOPV PCF

                    index += 1

    def _mask_ticker(self, ticker: str) -> str:
        # for cover call ETF, I don't see any need for now.
        return ticker

    def _WriteHsbcPcf(self, filename: str, filedate: str, pcfGenDbClnt, as_of_date_str) -> None:

        with open(filename, "w") as f:
            for line_index in range(8): # HSBC PCF has 8 lines of header, line index starts from 0
                if line_index == 0:
                    trade_date_dt = datetime.strptime(filedate, g_db_dateformat)
                    trade_date = trade_date_dt.strftime("%Y%m%d")
                    f.write(f'{line_index},{trade_date}\n')
                
                if line_index == 1:
                    ticker = self.bbgticker_.split(" ", 1)[0]
                    if ticker == '7347':
                        ticker = '9347' # to be compatible with current version
                    f.write(f'{line_index},{ticker}\n')

                if line_index == 2:
                    num_of_components = len(self.holdings_)
                    f.write(f'{line_index},{num_of_components}\n')

                if line_index == 3:
                    estimated_cash = round(self.estimated_cash_, 2)
                    f.write(f'{line_index},{estimated_cash}\n')

                if line_index == 4:
                    num_fund_shares = self.total_shares_
                    f.write(f'{line_index},{num_fund_shares}\n')

                if line_index == 5:
                    nav_per_share = self._formatFloatAsInt(float(self.nav_per_share_))
                    f.write(f'{line_index},{nav_per_share}\n')
                
                if line_index == 6:
                    basket_size = self._formatFloatAsInt(float(self.basket_size_))
                    f.write(f'{line_index},{basket_size}\n')
                
                if line_index == 7:
                    sql_holding = f"select Ticker, Share, Price, Ccy, Description from t_pcf_holding where TradarCode = '{self.tradar_code_}' and AsOfDate = '{as_of_date_str}' order by Ticker"
                    with pcfGenDbClnt.cursor() as cursor:
                        cursor.execute(sql_holding)
                        rows = cursor.fetchall()
                        rows = sorted(rows, key=lambda row: "EQUITY" in str(row[0]).upper())

                        for row in rows:
                            component_ticker = row[0]
                            share = self._formatFloatAsInt(float(row[1]))
                            contract_size = self.holdings_.loc[self.holdings_["Ticker"] == component_ticker, "Val_Point"].values[0]
                            # if comp_ticker endswith("EQUITY")
                            if component_ticker.upper().endswith(" EQUITY"):
                                hsbc_share = int(share * contract_size)
                            else:
                                hsbc_share = round(share * contract_size, 5)
                            comp_ticker = " ".join(component_ticker.split(" ")[:-1])
                            hsbc_ticker = self._mask_ticker(comp_ticker)
                            f.write(f'{line_index},{hsbc_ticker},{hsbc_share}\n')
            
            f.write("99\n")

    def _WriteCstnPcf(self, filename: str, filedate: str, pcfGenDbClnt, as_of_date_str) -> None:
        with open(filename, "w") as f:
            self._WritePcfHeader(f, filedate, False, pcfGenDbClnt, as_of_date_str)

            f.write(",Company Name,Currency,Price,Shares,ISIN,SEDOL,RIC,Contract Size,Bloomberg Ticker\n")
            # for debug
            # logger.info(f"holding: \n{self.holdings_}")

            # get holding level data from t_pcf_holding
            sql_holding = f"select Ticker, Share, Price, Ccy, Description from t_pcf_holding where TradarCode = '{self.tradar_code_}' and AsOfDate = '{as_of_date_str}'"
            with pcfGenDbClnt.cursor() as cursor:
                cursor.execute(sql_holding)
                rows = cursor.fetchall()

                index = 1
                for row in rows:
                    ticker = row[0]
                    share = self._formatFloatAsInt(float(row[1])) # convert 1.0 to 1, but if 1.23, keep 1.23
                    price = self._formatFloatAsInt(float(round(row[2],4))) # changed to 4 align with lni_pcf.py

                    ccy = row[3]
                    description = row[4]

                    isin = self.holdings_.loc[self.holdings_["Ticker"] == ticker, "Isin"].values[0]
                    if isin is None:
                        isin = '' # instead of write "none" in the pcf, write an empty string

                    ric = self.holdings_.loc[self.holdings_["Ticker"] == ticker, "RIC"].values[0]
                    masked_ric = self._mask_ticker(ric)
                    bbgticker = self.holdings_.loc[self.holdings_["Ticker"] == ticker, "BbgTicker"].values[0]
                    masked_bbgticker = self._mask_ticker(bbgticker)

                    contract_size_raw = self.holdings_.loc[self.holdings_["Ticker"] == ticker, 'Val_Point'].values[0]
                    contract_size = self._formatFloatAsInt(contract_size_raw) # convert 1.0 to 1, but if 1.23, keep 1.23
                    
                    f.write(f"{index},{description},{ccy},{price},{share},{isin},,{masked_ric},{contract_size},{masked_bbgticker}\n")
                    index += 1   

    # implememt GeneratePcf
    def GeneratePcf(self, riskDbClnt, opsDbClnt, pcfGenDbClnt, pcf_full_path: str, pcf_release_path: str, release=True, force=False) -> PCF_STATUS:
        # before generate the PCF, clear up pcf_files, otherwise will keep on copying file to release folder
        self.pcf_files_.clear()

        if self.expected_nav_date_ == None:
            self.expected_nav_date_ = Pcf.ExpectedNavDate(opsDbClnt, self.cdb_fund_id_, self.pcf_file_date_, self.tradar_code_, self.pcf_holidays_)# retry to get the expected NAV date
            if self.expected_nav_date_ == None:
                return PCF_STATUS.Pending # wait for next retry, Maybe NAV is late or ops missing holiday entry
            
        pcf_status_to, nav1_timestamp_dt = self._CheckNav1Status(opsDbClnt, pcfGenDbClnt)
        
        if pcf_status_to == PCF_STATUS.Pending and nav1_timestamp_dt is None:
            return pcf_status_to # no signoff case
        
        if pcf_status_to == PCF_STATUS.No:
            if force == False:
                return pcf_status_to # nothing changed
            else:
                self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav1_timestamp_dt)
                pcf_status_to = PCF_STATUS.Done # force to generate pcf even no change
                
        self._SyncPcfData(riskDbClnt, opsDbClnt, pcfGenDbClnt)
        
        pcf_filenames = self._GenPcfFileName(pcfGenDbClnt)

        # generate the PCF file according to pcfGenDBClnt t_pcf_fund and t_pcf_holding
        # write to pcf_full_path line by line
        try:
            as_of_date_str = self.expected_nav_date_.strftime(g_db_dateformat)
            if PcfType.PCF.value in pcf_filenames:
                pcf_full_name = pcf_full_path + pcf_filenames[PcfType.PCF.value]
                self._WritePcf(pcf_full_name, self.pcf_file_date_, pcfGenDbClnt, as_of_date_str)
                self.pcf_files_.append(pcf_full_name)
                logger.info(f"PCF file generated: {pcf_full_name}")

            if PcfType.IOPVPCF.value in pcf_filenames:
                iopv_pcf_full_name = pcf_full_path + pcf_filenames[PcfType.IOPVPCF.value]
                self._WriteIopvPcf(iopv_pcf_full_name, self.pcf_file_date_, pcfGenDbClnt, as_of_date_str)
                self.pcf_files_.append(iopv_pcf_full_name)
                logger.info(f"IOPV PCF file generated: {iopv_pcf_full_name}")
            
            if PcfType.HSBCPCF.value in pcf_filenames:
                hsbc_pcf_full_name = pcf_full_path + pcf_filenames[PcfType.HSBCPCF.value]
                self._WriteHsbcPcf(hsbc_pcf_full_name, self.pcf_file_date_, pcfGenDbClnt, as_of_date_str)
                self.pcf_files_.append(hsbc_pcf_full_name)
                logger.info(f"HSBC PCF file generated: {hsbc_pcf_full_name}")
            
            if PcfType.CSTNPCF.value in pcf_filenames:
                cstn_pcf_full_name = pcf_full_path + pcf_filenames[PcfType.CSTNPCF.value]
                self._WriteCstnPcf(cstn_pcf_full_name, self.pcf_file_date_, pcfGenDbClnt, as_of_date_str)
                self.pcf_files_.append(cstn_pcf_full_name)
                logger.info(f"CSTN PCF file generated: {cstn_pcf_full_name}")

            if release:
                self.ReleasePcfFiles(pcf_release_path)
            
            self._UpdatePcfStatus(pcfGenDbClnt, pcf_status_to, nav1_timestamp_dt, as_of_date_str) # inside is insert a new line

            logger.info(f"PCF generation completed for {self.tradar_code_} on {self.pcf_file_date_} status={pcf_status_to.value}")
            return pcf_status_to
        except Exception as e:
            errmsg = f"GeneratePcf fail {self.tradar_code_} as of {self.pcf_file_date_}: {e}"
            logger.error(errmsg)
            raise Exception("GeneratePcf failed")

    def _newPcfStatus(self, pcfGenDbClnt, tradar_code, status, nav2_timestamp_dt):
        
        try:
            now = datetime.now().strftime(g_opsdb_dateformat + " %H:%M:%S")

            # convert nav2_timestamp to standard format when available
            nav2_timestamp = None
            if nav2_timestamp_dt is not None:
                nav2_timestamp = nav2_timestamp_dt.strftime(g_opsdb_dateformat + " %H:%M:%S")

            insert_sql = """
                INSERT INTO t_pcf_status (TradarCode, FileDate, Status, [TimeStamp], Nav2TimeStamp, NavDate)
                VALUES (%s, %s, %s, %s, %s, %s)
            """
            with pcfGenDbClnt.cursor() as cursor:
                cursor.execute(
                    insert_sql,
                    (tradar_code, self.pcf_file_date_, status.value, now, nav2_timestamp, '00000000'), # initial insert, do not know the nav date yet, so set to '00000000'
                )
                pcfGenDbClnt.commit()
                
        except Exception as e:
            logger.exception(f"newPcfStatus: {e}")
            logger.info(f"Sql: {insert_sql}")


    def _CheckNav1Status(self, opsDbClnt, pcfGenDbClnt):

        try:
            asofdate = self.expected_nav_date_.strftime(g_opsdb_dateformat)
            sql = f"select version_date from fund_level_data where acct_book_id = '{self.cdb_fund_id_}' and acct_book_class_id = '{self.cdb_listed_class_id_}' and recon_type = '{self.RECON_TYPE}' and as_of_date = '{asofdate}'"
            with opsDbClnt.cursor() as cursor:
                cursor.execute(sql)
                row = cursor.fetchone()
                if row is None:
                    # logger.warning(f"NAV1 of {self.tradar_code_} as of nav date {asofdate} is not signoff")
                    # logger.info(f"Check Signoff Sql: {sql}")
                    nav1_version_date = None
                    nav1_version_date_dt = None
                    # check NAV1 holidday, if it is holiday, treat it as signoff.
                    # nav1_version_date_dt = today 00:00:00 never changed
                else:
                    nav1_version_date = row[0]
                    nav1_version_date_std = self._NavTimestampStdFormat(nav1_version_date)
                    nav1_version_date_dt = datetime.strptime(nav1_version_date_std, g_opsdb_dateformat + " %H:%M:%S")

            # to find the latest status and Nav2TimeStamp, use Id desc not the timestamp
            sql2 = f"select Top 1 Nav2TimeStamp, Status from t_pcf_status where TradarCode = '{self.tradar_code_}' and FileDate = '{self.pcf_file_date_}' order by Id desc"
            
            with pcfGenDbClnt.cursor() as cursor:
                cursor.execute(sql2)
                row = cursor.fetchone()
                
                # from the sql, it is in datetime type
                nav1_timestamp_dt = row[0] if row else None # for equity pcf, we using NAV1, although column name in t_pcf_status is called Nav2TimeStamp
                pcf_status = row[1] if row else None

            # if not force case, will continue below
            if nav1_version_date_dt is None:
                logger.warning(f"_CheckNav1Status:{self.tradar_code_} as of nav date {asofdate} is not signoff")
                logger.info(f"check Signoff Sql: {sql}")
                going_to_status = PCF_STATUS.Pending
                nav1_timestamp_dt = None

                if pcf_status is None:
                    logger.info(f"_CheckNav1Status: first check {self.tradar_code_}, nav date {asofdate}")
                    self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav1_timestamp_dt) # will persist to Nav2TimeStamp column in t_pcf_status
                elif pcf_status == PCF_STATUS.Done.value:
                    logger.info(f"_CheckNav1Status: {self.tradar_code_} as of nav date {asofdate} is unsignoff")
                    self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav1_timestamp_dt)
            else:
                # now need to check whether NAV1 revise or first time generate PCF.            
                if pcf_status is None:
                    logger.info(f"_CheckNav1Status: first check {self.tradar_code_}, nav date {asofdate}")
                    going_to_status = PCF_STATUS.Done
                    nav1_timestamp_dt = nav1_version_date_dt
                    self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav1_timestamp_dt) # will persist to Nav2TimeStamp column in t_pcf_status
                elif pcf_status == PCF_STATUS.Pending.value:
                    going_to_status = PCF_STATUS.Done
                    nav1_timestamp_dt = nav1_version_date_dt
                    # we don't insert another pending status
                else:
                    # here pcf_Status should be Done
                    if nav1_version_date_dt > nav1_timestamp_dt:
                        going_to_status = PCF_STATUS.Revise 
                        nav1_timestamp_dt = nav1_version_date_dt   
                        self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav1_timestamp_dt) # will persist to Nav2TimeStamp column in t_pcf_status
                    else:
                        # NAV1 is not changed.
                        going_to_status = PCF_STATUS.No
                        nav1_timestamp_dt = nav1_version_date_dt

        except Exception as e:
            logger.exception(f"CheckNav1Status ex: {e}")
            logger.info(f"Sql: {sql}")

        return going_to_status, nav1_timestamp_dt
    
    def _NavTimestampStdFormat(self, nav_timestamp) -> str:
        if isinstance(nav_timestamp, str):
            ts = nav_timestamp.strip().replace("T", " ")
            if "+" in ts:
                ts = ts.split("+", 1)[0].strip()

            if "." in ts:
                ts = ts.split(".", 1)[0].strip()

            nav_timestamp = ts

        return nav_timestamp
    
    def _UpdatePcfStatus(self, pcfGenDbClnt, status, nav1_timestamp_dt, as_of_date_str) -> None:
        
        try:
            if status != PCF_STATUS.No:
                now = datetime.now().strftime(g_opsdb_dateformat + " %H:%M:%S")

                # convert nav2_timestamp to standard format when available
                nav1_timestamp = None
                if nav1_timestamp_dt is not None:
                    nav1_timestamp = nav1_timestamp_dt.strftime(g_opsdb_dateformat + " %H:%M:%S")

                # keep the column name Nav2TimeStamp in t_pcf_status, although we are writing NAV1 timestamp into it.
                insert_sql = """
                    INSERT INTO t_pcf_status (TradarCode, FileDate, Status, [TimeStamp], Nav2TimeStamp, NavDate)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """
                with pcfGenDbClnt.cursor() as cursor:
                    cursor.execute(
                        insert_sql,
                        (self.tradar_code_, self.pcf_file_date_, status.value, now, nav1_timestamp, as_of_date_str),
                    )
                    pcfGenDbClnt.commit()
                        
        except Exception as e:
            logger.exception(f"_UpdatePcfStatus: {e}")
            logger.info(f"Sql: {insert_sql}")
        
