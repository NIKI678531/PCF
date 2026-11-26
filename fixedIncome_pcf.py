from datetime import datetime, timedelta

import pandas as pd
from pcf import Pcf, g_db_dateformat, g_riskdb_dateformat, g_opsdb_dateformat, PcfType, PCF_STATUS

import logging

logger = logging.getLogger('Main')

class FixedIncomePcf(Pcf):
    CLASSIFICATION_INVESTMENTS = "Investments"
    RECON_TYPE = "NAV1"
    SECURITY_TYPES_WITH_FACE_VALUE = ("OTC Note", "CP&CD", "Government Bound")

    def __init__(self, tradar_code: str, cdb_fund_id: str, pcf_date: str, pcfGenDbClnt, opsDbClnt):
        super().__init__(tradar_code, cdb_fund_id, pcf_date, pcfGenDbClnt, opsDbClnt)

        self.holding_classification_ = [self.CLASSIFICATION_INVESTMENTS]

        self.holdings_ = None

    # different from cc_pcf
    def _getHoldingIdentity(self, ticker: str):
        # find the "Ticker", "ISIN" from self.holdings_
        row = self.holdings_.loc[self.holdings_["Ticker"] == ticker]

        if not row.empty:
            bbgticker = ticker
            ric = 'CN' + ticker

            isin = row["ISIN"].values[0]
        else:
            bbgticker = None
            ric = None
            isin = None

            logger.error(f"_getHoldingIdentity: missing {ticker} self.holdings_.")

        return bbgticker, ric, isin

    # return dividend in the fund ccy. ex_date = as_of_date of NAV
    def _getDividend(self, opsDbClnt, pcfGenDbClnt):
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
        
    def _SyncPcfComponents(self, riskDbClnt, opsDbClnt, pcfGenDbClnt, as_of_date: datetime) -> bool:
        
        with riskDbClnt.cursor() as cursor:
            # MMF using NAV1 for holding
            holdingid_sql = f"select acct_holding_id from account_holdings where acct_book_id = '{self.cdb_fund_id_}' and as_of_date = '{as_of_date.strftime(g_riskdb_dateformat)}' and holding_type = '{self.RECON_TYPE}' and is_latest = 1"
            cursor.execute(holdingid_sql)
            row = cursor.fetchone()
            if row is None:
                logger.info(f"_SyncPcfComponent Sql: {holdingid_sql}")
                logger.error(f"No {self.RECON_TYPE} holding for fund {self.tradar_code_} as of date {as_of_date.strftime(g_db_dateformat)}.")
                return False
            
            acct_holding_id = row[0]

            classification_str = ''
            for classification in self.holding_classification_:
                classification_str += f"'{classification}',"
            classification_str = classification_str[:-1] # remove the last comma

            # However, from riskdb, we can replace it with classification in ('Investments')
            sql2 = f"select ticker, isin, amount, price, ccy, fx_lc_fc, security_desc, fund_ccy, classification, security_type, exposure_nav_percent, coupon, val_point from account_holdings_data where acct_holding_id = '{acct_holding_id}' and classification in ({classification_str})"
            cursor.execute(sql2)
            rows = cursor.fetchall()

            # save the result in a pandas dataframe for later use in PCF generation.
            holdings = pd.DataFrame(rows, columns=['Ticker', 'ISIN', 'Amount', 'Price', 'CCY', 'FX_LC_FC', 'Security_Desc', 'Fund_CCY', 'Classification', 'Security_Type', 'Exposure_NAV_Percent', 
                                                   'Coupon', 'Val_Point'])
            holdings[['Amount', 'Price', 'FX_LC_FC', 'Exposure_NAV_Percent', 'Coupon', 'Val_Point']] = holdings[['Amount', 'Price', 'FX_LC_FC', 'Exposure_NAV_Percent', 'Coupon', 'Val_Point']].astype(float)
            # ticker = "SOBRKB2L Index"

            if holdings.empty:
                logger.info(f"_SyncPcfComponent Sql2: {sql2}")
                logger.error(f"No holding with classification '{self.holding_classification_}' for fund {self.tradar_code_} as of date {as_of_date.strftime(g_db_dateformat)}.")
                return False

            self.holdings_ = holdings

            self.fund_ccy_ = self.holdings_["Fund_CCY"].iloc[0] if not self.holdings_.empty else None # fund_ccy should be same for all rows.

        with opsDbClnt.cursor() as cursor:
            # for MMF using NAV1
            sql_list_shares= f"select pma_units from fund_level_data where acct_book_id = '{self.cdb_fund_id_}' and acct_book_class_id = '{self.cdb_listed_class_id_}' and recon_type = '{self.RECON_TYPE}' and as_of_date = '{as_of_date.strftime(g_opsdb_dateformat)}'"
            # sql_total_fund_units= f"select sum(pma_units) as total_fund_units from fund_level_data where acct_book_id = '{self.cdb_fund_id_}' and recon_type = '{self.RECON_TYPE}' and as_of_date = '{as_of_date.strftime(g_opsdb_dateformat)}'"

            cursor.execute(sql_list_shares)
            row = cursor.fetchone()
            if row is None:
                logger.info(f"_SyncPcfComponent Sql_total_fund_units: {sql_list_shares}")
                logger.error(f"No fund level data found for fund {self.tradar_code_} {self.cdb_fund_id_} as of date {as_of_date.strftime(g_db_dateformat)} using NAV1.")
                return False
            
            list_shares = float(row[0]) # listed shares units

        # for MMF, there is not Contract_Size, but keep it here and set to 1 for minimum changes compared to other funds.
        # self.holdings_["Contract_Size"] = 1.0 # here it is a column named "Contract_Size"
        
        # MMF hard code the BbgTicker, RIC, Isin
        self.holdings_["BbgTicker"] = ''
        self.holdings_["RIC"] = ''
        self.holdings_["Isin"] = ''

        for ticker in self.holdings_["Ticker"]:
            bbgticker, ric, isin = self._getHoldingIdentity(ticker)
            # in Fixed Income ETF, we don't need the RIC and BbgTicker for bonds

            # self.holdings_.loc[self.holdings_["Ticker"] == ticker, "BbgTicker"] = bbgticker
            self.holdings_.loc[self.holdings_["Ticker"] == ticker, "RIC"] = ric
            self.holdings_.loc[self.holdings_["Ticker"] == ticker, "Isin"] = isin
        
        as_of_date_str = as_of_date.strftime(g_db_dateformat)

        # calculate the weight of listed share class = listed_nav / total_nav
        listShareWeight = self.ComponentListWeight(riskDbClnt, opsDbClnt, as_of_date.strftime(g_opsdb_dateformat)) # this is the weight of the listed share class in the fund, used to calculate the amount of each component in the listed share class.
        
        logger.info(f"SyncPcfComponents: {self.tradar_code_}, {as_of_date_str}, listshares={list_shares}, listShareWeight={listShareWeight}, basket_size={self.basket_size_}")

        # logger.info(f"holding: \n{self.holdings_}")

        # Calculate shares_per_basket for each ticker. shares_per_basket = "Shares" column in the PCF
        for holding_index, holding_row in self.holdings_.iterrows():
            list_weighted_shares = holding_row['Amount'] * listShareWeight
            list_weighted_shares_per_basket = round(list_weighted_shares / (list_shares / self.basket_size_), 4)

            self.holdings_.at[holding_index, 'shares_per_basket'] = list_weighted_shares_per_basket

        try:
            # before insert, delete existing records for the same TradarCode and AsOfDate to avoid duplicate records.
            for table in ["t_pcf_holding", "t_iopvpcf_holding"]:
                  logger.info(f"To check calculation, ( select * from {table} where TradarCode = '{self.tradar_code_}' AND AsOfDate = '{as_of_date_str}' )")

                  with pcfGenDbClnt.cursor() as cursor:
                    cursor.execute(f"DELETE FROM {table} WHERE TradarCode = '{self.tradar_code_}' AND AsOfDate = '{as_of_date_str}'")

                    for index, row in self.holdings_.iterrows():
                        ticker = row['Ticker']
                        shares_per_basket = row['shares_per_basket'] # [(amt of this component in this fund)/total shares]*basket_size = shares per basket
                        price = row['Price']
                        coupon = row['Coupon']
                        valpoint = row['Val_Point']

                        logger.info(f"SyncPcfComponents: table={table}, tradar_code={self.tradar_code_}, ticker={ticker}, share_per_basket={shares_per_basket}, basket_size={self.basket_size_}")

                        cursor.execute(f"""
                            INSERT INTO {table} (TradarCode, Ticker, Share, Price, Ccy, FxRate, Description, AsOfDate, Coupon, ValPoint)
                            VALUES ('{self.tradar_code_}', '{ticker}', {shares_per_basket}, {price}, '{row['CCY']}', {row['FX_LC_FC']}, '{row['Security_Desc']}', '{as_of_date_str}', {coupon}, {valpoint})
                        """)
            pcfGenDbClnt.commit()
        except Exception as e:
            logger.exception(f"SyncPcfComponents persist {table} for fund {self.tradar_code_} as of date {as_of_date_str}: {e}")
            return False
        
        return True

    def _SyncPcfFundData(self, riskDbClnt, opsDbClnt, pcfGenDbClnt, as_of_date: datetime) -> bool:

        # Sync components first, then sync fund level data. Because fund level data need count of holdings.
        if self._SyncPcfComponents(riskDbClnt, opsDbClnt, pcfGenDbClnt, as_of_date):
            logger.info(f"SyncPcfComponents succeeded. Now sync Pcf fund level data.") 

            ops_table = "fund_level_data"

            sql_nav1 = f"""select as_of_date, recon_type, acct_book_id as fund_id, acct_book_class_id as class_id, 
                      pma_nav as nav, pma_nav_per_share as nav_per_share, pma_units as units from {ops_table}
                      where recon_type = '{self.RECON_TYPE}' and as_of_date = '{as_of_date.strftime(g_riskdb_dateformat)}' and is_nav_holiday = 0 
                      and acct_book_id = '{self.cdb_fund_id_}' and acct_book_class_id = '{self.cdb_listed_class_id_}'
                      """

            with opsDbClnt.cursor() as cursor:
                cursor.execute(sql_nav1)
                row = cursor.fetchone()
                if row is None:
                    logger.info(f"Sql: {sql_nav1}")
                    logger.error(f"No fund level data found for fund {self.tradar_code_} {self.cdb_fund_id_} as of date {as_of_date.strftime(g_db_dateformat)}.")
                    return False
                
                nav1 = float(row[4])
                nav_per_share1 = float(row[5])
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
                fee_rate = float(row[1])
                fee_fixed = float(row[2])
            
            # calculate the estimated cash
            pcf_file_date_dt = datetime.strptime(self.pcf_file_date_, g_db_dateformat)
            day_diff = (pcf_file_date_dt - as_of_date).days
            
            component_value = 0.0
            coupon_value = 0.0
            for index, row in self.holdings_.iterrows():
                component_share_per_basket = row['shares_per_basket'] # this is weighted for listed shared class
                component_price = round(row['Price'], 4)
                component_fx = row['FX_LC_FC']
                coupon = row['Coupon']
                val_point = row['Val_Point']
                

                component_value += (component_share_per_basket * component_price) * val_point/(component_fx)
                coupon_value += (component_share_per_basket * coupon)
            
            component_value = round(component_value, 4)
            nav1 = round(nav1, 2)

            dividend = self._getDividend(opsDbClnt, pcfGenDbClnt)

            if self.total_net_assets_ is None:
                raise ValueError(f"Total net assets is None for {self.tradar_code_}. Check whether have called calculateWeight or ComponentListWeight already.")
            else:
                netassetvalue = self.total_net_assets_

            listnavpershare_ration = nav_per_share1 / netassetvalue

            # note: *val_point equivalent to /100. There is assumption here is all val_points are same. This is true for HK-UST20
            a = (((netassetvalue*(1-(fee_rate/365)*day_diff) - fee_fixed * day_diff)*listnavpershare_ration)-dividend)*basket_size
            b = (coupon_value*val_point/365*day_diff)
            estimated_cash_num = a - component_value + b
            
            estimated_cash = round(estimated_cash_num, 4)

            logger.info(f"{self.tradar_code_} netassetvalue={netassetvalue}, nav_per_share={nav_per_share1}, basket_size={basket_size}, component_value={component_value}, coupon_value={coupon_value}, dividend={dividend}, daydiff={day_diff}")
            logger.info(f"{self.tradar_code_} estimated_cash: {estimated_cash}")
        else:
            logger.error(f"SyncPcfFundData: SyncPcfComponents failed. Sync pcf components then sync fund level data")
            return False
        
        as_of_date_str = as_of_date.strftime(g_db_dateformat)
        try:

            for table in ["t_pcf_fund", "t_iopvpcf_fund"]:
                with pcfGenDbClnt.cursor() as cursor:
                    # before insert, delete existing records for the same TradarCode and AsOfDate to avoid duplicate records.
                    cursor.execute(f"DELETE FROM {table} WHERE TradarCode = '{self.tradar_code_}' AND AsOfDate = '{as_of_date_str}'")

                    # note: here using NAV1 for fund level data
                    cursor.execute(f"""
                        INSERT INTO {table} (TradarCode, NavPerShare, EstimateCash, TotalShares, AsOfDate, FundCcy)
                        VALUES ('{self.tradar_code_}', {nav_per_share1}, {estimated_cash}, {units1}, '{as_of_date_str}', '{self.fund_ccy_}')
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
            
        # if self.nav_per_share_ is None or self.estimated_cash_ is None or self.total_shares_ is None:
        # as_of_date_str = self.expected_nav_date_.strftime(g_db_dateformat)
        sql_fund = f"select NavPerShare, EstimateCash, TotalShares, FundCcy from {table} where TradarCode = '{self.tradar_code_}' and AsOfDate = '{as_of_date_str}'"
        with pcfGenDbClnt.cursor() as cursor:
            cursor.execute(sql_fund)
            row = cursor.fetchone()
            if row is None:
                logger.info(f"Sql: {sql_fund}")
                logger.error(f"WritePcfHeader: No fund data for {self.tradar_code_} as of {filedate}")
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

            # f.write(",Company Name,Currency,Price,Shares,ISIN,SEDOL,RIC,Contract Size,Bloomberg Ticker\n")
            f.write(",Company Name,Currency,Price,Shares,ISIN,SEDOL,RIC\n") # specific for Fixed Income ETF, there is not Contract Size and Bloomberg Ticker.

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

                    f.write(f"{index},{description},{ccy},{price},{share},{isin},,{ric}\n")
                    index += 1

    def _WriteIopvPcf(self, filename: str, filedate: str, pcfGenDbClnt, as_of_date_str) -> None:
        with open(filename, "w") as f:
            self._WritePcfHeader(f, filedate, True, pcfGenDbClnt, as_of_date_str)

            f.write(",Company Name,Currency,Price,Shares,ISIN,SEDOL,RIC\n")
            # for debug
            # logger.info(f"holding: \n{self.holdings_}")

            # get holding level data from t_pcf_holding
            sql_holding = f"select Ticker, Share, Price, Ccy, Description from t_iopvpcf_holding where TradarCode = '{self.tradar_code_}' and AsOfDate = '{as_of_date_str}'"
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

                    f.write(f"{index},{description},{ccy},{price},{share},{isin},,{ric}\n") # exclude the contract size column for IOPV PCF

                    index += 1

    def _mask_ticker(self, ticker: str) -> str:
        # Mask detail ticker in hsbc pcf file
        return ''

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
                    # as_of_date_str = self.expected_nav_date_.strftime(g_db_dateformat)
                    # get holding level data from t_pcf_holding
                    sql_holding = f"select Ticker, Share, Price, Ccy, Description from t_pcf_holding where TradarCode = '{self.tradar_code_}' and AsOfDate = '{as_of_date_str}' order by Ticker"
                    with pcfGenDbClnt.cursor() as cursor:
                        cursor.execute(sql_holding)
                        rows = cursor.fetchall()
                        rows = sorted(rows, key=lambda row: "EQUITY" in str(row[0]).upper())

                        for row in rows:
                            component_ticker = row[0]
                            share = self._formatFloatAsInt(float(row[1]))
                            contract_size = self.holdings_.loc[self.holdings_["Ticker"] == component_ticker, 'Val_Point'].values[0]
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
        logger.error(f"Fixed Income ETF does not support CSTN PCF generation. Please check the fund {self.tradar_code_}.")

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
                
        if self._SyncPcfData(riskDbClnt, opsDbClnt, pcfGenDbClnt) == False:
            logger.info(f"Failed to sync PCF data for {self.tradar_code_} on {self.pcf_file_date_}. Status set to Pending. Wait for next retry.")
            pcf_status_to = PCF_STATUS.Pending
            return pcf_status_to
        
        pcf_filenames = self._GenPcfFileName(pcfGenDbClnt)

        # generate the PCF file according to pcfGenDBClnt t_pcf_fund and t_pcf_holding
        # write to pcf_full_path line by line
        
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
            
        # but in the table the column is named Nav2TimeStamp, but for MMF, we store Nav1TimeStamp.
        self._UpdatePcfStatus(pcfGenDbClnt, pcf_status_to, nav1_timestamp_dt, as_of_date_str) # inside is insert a new line

        logger.info(f"PCF generation completed for {self.tradar_code_} on {self.pcf_file_date_} status={pcf_status_to.value}")
        return pcf_status_to
        

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
            # expected_nav_date_ based on t_nav_holiday find T-1 day. Don't check is_nav_holiday in fund_level_data.
            # using fund_level_data is not reliable. In case of custodian delay, there is not entry in the db. Then we cannot know it is a holiday or not. 
            asofdate = self.expected_nav_date_.strftime(g_opsdb_dateformat)
            sql = f"select version_date from fund_level_data where acct_book_id = '{self.cdb_fund_id_}' and acct_book_class_id = '{self.cdb_listed_class_id_}' and recon_type = '{self.RECON_TYPE}' and as_of_date = '{asofdate}'"
            with opsDbClnt.cursor() as cursor:
                cursor.execute(sql)
                row = cursor.fetchone()
                if row is None:
                    nav1_version_date = None
                    nav1_version_date_dt = None
                else:
                    nav1_version_date = row[0]
                    nav1_version_date_std = self._Nav1TimestampStdFormat(nav1_version_date)
                    nav1_version_date_dt = datetime.strptime(nav1_version_date_std, g_opsdb_dateformat + " %H:%M:%S")

            # to find the latest status and Nav2TimeStamp, use Id desc not the timestamp
            sql2 = f"select Top 1 Nav2TimeStamp, Status from t_pcf_status where TradarCode = '{self.tradar_code_}' and FileDate = '{self.pcf_file_date_}' order by Id desc"
            
            with pcfGenDbClnt.cursor() as cursor:
                cursor.execute(sql2)
                row = cursor.fetchone()
                
                # from the sql, it is in datetime type
                nav1_timestamp_dt = row[0] if row else None # the db table columns is Nav2TimeStamp, but for MMF, we store Nav1TimeStamp.
                pcf_status = row[1] if row else None

            # if not force case, will continue below
            if nav1_version_date_dt is None:
                logger.warning(f"_CheckNav1Status:{self.tradar_code_} as of nav date {asofdate} is not signoff")
                logger.info(f"check Signoff Sql: {sql}")
                going_to_status = PCF_STATUS.Pending
                nav1_timestamp_dt = None

                if pcf_status is None:
                    logger.info(f"_CheckNav1Status: first check {self.tradar_code_}, nav date {asofdate}, insert new record")
                    self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav1_timestamp_dt)
                elif pcf_status == PCF_STATUS.Done.value:
                    logger.info(f"_CheckNav1Status: {self.tradar_code_}, nav date {asofdate} is unsignoff")
                    self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav1_timestamp_dt)
            else:
                # now need to check whether revise or first time generate PCF.            
                if pcf_status is None:
                    logger.info(f"_CheckNav1Status: first check {self.tradar_code_}, nav date {asofdate}, insert new record")
                    going_to_status = PCF_STATUS.Done
                    nav1_timestamp_dt = nav1_version_date_dt
                    self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav1_timestamp_dt)
                elif pcf_status == PCF_STATUS.Pending.value:
                    going_to_status = PCF_STATUS.Done
                    nav1_timestamp_dt = nav1_version_date_dt
                    # we don't insert another pending status
                else:
                    # here pcf_Status should be Done
                    if nav1_version_date_dt > nav1_timestamp_dt:
                        going_to_status = PCF_STATUS.Revise 
                        nav1_timestamp_dt = nav1_version_date_dt   
                        self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav1_timestamp_dt)
                    else:
                        # NAV1 is not changed.
                        going_to_status = PCF_STATUS.No
                        nav1_timestamp_dt = nav1_version_date_dt

        except Exception as e:
            logger.exception(f"CheckNav1Status ex: {e}")
            logger.info(f"Sql: {sql}")

        return going_to_status, nav1_timestamp_dt
    
    def _Nav1TimestampStdFormat(self, nav1_timestamp) -> str:
        if isinstance(nav1_timestamp, str):
            ts = nav1_timestamp.strip().replace("T", " ")
            if "+" in ts:
                ts = ts.split("+", 1)[0].strip()

            if "." in ts:
                ts = ts.split(".", 1)[0].strip()

            nav1_timestamp = ts

        # logger.info(f"_Nav1TimestampStdFormat: nav1_timestamp={nav1_timestamp}")
        return nav1_timestamp
    
    def _UpdatePcfStatus(self, pcfGenDbClnt, status, nav1_timestamp_dt, as_of_date_str) -> None:
        
        try:
            if status != PCF_STATUS.No:
                now = datetime.now().strftime(g_opsdb_dateformat + " %H:%M:%S")

                # convert nav1_timestamp to standard format when available
                nav1_timestamp = None
                if nav1_timestamp_dt is not None:
                    nav1_timestamp = nav1_timestamp_dt.strftime(g_opsdb_dateformat + " %H:%M:%S")

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
        
