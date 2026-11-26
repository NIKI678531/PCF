from datetime import datetime
import re

import pandas as pd
from pcf import Pcf, g_db_dateformat, g_riskdb_dateformat, g_opsdb_dateformat, PcfType, PCF_STATUS

import logging

logger = logging.getLogger('Main')

class LnIPcf(Pcf):
    CLASSIFICATION_FUTURES = "Futures Contracts"
    CLASSIFICATION_SWAP = "Contracts for Difference" # default of LnI ETF
    CLASSIFICATION_INVESTMENTS = "Investments"
    RECON_TYPE = "NAV2" # by default in LnI we are using the NAV2

    def __init__(self, tradar_code: str, cdb_fund_id: str, pcf_date: str, pcfGenDbClnt, opsDbClnt):
        super().__init__(tradar_code, cdb_fund_id, pcf_date, pcfGenDbClnt, opsDbClnt)

        self.EquityFut_SwapBasis_ = ['HK-SMSN-2I', 'HK-SMSN-2L', 'HK-HST-L', 'HK-HST-2I']  # Equity Future + Swap Basis
        self.IndexFut_Swap_ = ['HK-HSI-L', 'HK-HSI-I', 'HK-HSI-2I']  # Index Future + Index Swap
        self.IndexFut_ = ['HK-GLD-L', 'HK-BTCF-I', 'HK-NDX-L', 'HK-NDX-2I', 'HK-BTCFUT', 'HK-DCRFUT', 'HK-NIK-L', 'HK-NIK-2I', 'HK-HCE-L', 'HK-HCE-2I']  # Index Future
        self.EquityFutSwap_Option_ = ['HK-SKHY-2L']
        self.EquityCFD_ = ['HK-KIOX-125L']  # Equity CFD

        # others LnI which will go default calculation:
        # self.Others_ = [
        #     'HK-BRK-2L',
        #     'HK-COIN-2I',
        #     'HK-COIN-2L',
        #     'HK-CS3-L',
        #     'HK-MSTR-2I',
        #     'HK-MSTR-2L',
        #     'HK-NVDA-2I',
        #     'HK-NVDA-2L',
        #     'HK-TSLA-2I',
        #     'HK-TSLA-2L'
        # ]

        self.PcfDisplayComponent_PatternMap_ = {'HK-GLD-L': 'GC[A-Z][0-9] COMDTY'}
        self.IOPVPcfDisplayComponent_PatternMap_ = {}


        if self.tradar_code_ in self.IndexFut_Swap_:
            self.holding_classification_ = [self.CLASSIFICATION_FUTURES, self.CLASSIFICATION_SWAP] 
        elif self.tradar_code_ in self.IndexFut_:
            self.holding_classification_ = [self.CLASSIFICATION_FUTURES] 
        elif self.tradar_code_ in self.EquityFut_SwapBasis_:
            self.holding_classification_ = [self.CLASSIFICATION_SWAP]
        elif self.tradar_code_ in self.EquityFutSwap_Option_:
            self.holding_classification_ = [self.CLASSIFICATION_SWAP, self.CLASSIFICATION_INVESTMENTS] # currently, ops classify option as Investments, in future, 7709 add Equity HedgeFund, I expect it is classed as Investments also same as Cover Call ETF
        else:
            self.holding_classification_ = [self.CLASSIFICATION_SWAP] # Index Swap

        self.holdings_ = None
        self.expo2_holdings_ = None # keep raw data from exposure2, because in 7709, the price will be adjusted by swapbasis. However, we need the row price for iopv share calculation.

        self.swapbasis_total_ = 0.0 # used in EquityFut_SwapBasis fund to calculate the price
        if self.tradar_code_ in ['HK-SMSN-2I', 'HK-SMSN-2L']:
            self.SL_key_ = "zSwapBasis%" # used in EquityFut_SwapBasis fund to adjust the price
        elif self.tradar_code_ in ['HK-HST-L', 'HK-HST-2I']:
            self.SL_key_ = "zSL%" # used in EquityFut_SwapBasis fund to calculate the price
        elif self.tradar_code_ in ['HK-SKHY-2L']:
            self.SL_key_ = "zSwapBasis%" # used in EquityFutSwap_Option fund to calculate the price
        elif self.tradar_code_ in self.EquityFut_SwapBasis_ + self.EquityFutSwap_Option_:
            raise Exception(f"{self.tradar_code_} belongs to EquityFut_SwapBasis_ but is missing SL_Key.")
        
    def _MergeHolding(self, holding_df: pd.DataFrame) -> pd.DataFrame:
        # For rows with the same Ticker: SUM numeric position fields, keep "first item" for other columns.
        # Build the agg spec dynamically so adding/removing columns upstream doesn't break this.

        sum_columns = {'Amount', 'Exposure_NAV_Percent'}

        agg_spec = {
            col: ("sum" if col in sum_columns else "first")
            for col in holding_df.columns if col != "Ticker"
        }
        merged_df = holding_df.groupby(["Ticker"], as_index=False, sort=False).agg(agg_spec)
 
        # Preserve original column order.
        return merged_df[holding_df.columns]

    def _ConvertToContractSizeTicker(self, ticker: str):
        # for option ticker
        option_match = re.match(r"^(.+) \d{2}/\d{2}/\d{2} ([CP])\d+ EQUITY$", ticker, re.IGNORECASE)
        if option_match:
            return f"{option_match.group(1)} X {option_match.group(2).upper()}X EQUITY"

        ticker_parts = ticker.split(" ", 1)

        # handle future and also 00593=U6 KS EQUITY => 00593=U6, KS EQUITY
        if len(ticker_parts) == 2:
            ticker_code, market_sector = ticker_parts
            contract_match = re.match(r"^(.+)[FGHJKMNQUVXZ]\d$", ticker_code, re.IGNORECASE)
            if contract_match:
                return f"{contract_match.group(1)}XX {market_sector}"

        return ticker

    def _HoldingEquityCall(self) -> bool:
        # check if any ticker in self.holdings_ is an equity call option, e.g. "000660 KS 23/08/25 C50000 EQUITY"
        for sec_type in self.holdings_['Security_Type']:
            if sec_type == 'Equity Call':
                return True
        return False

    def _DerivedRic(self, bbgticker: str) -> str:
        CaptialTicker = bbgticker.upper()
        ric = None
        if CaptialTicker.endswith(" COMDTY"): # HK-GOLD-L
            # e.g. XDTZ6 Comdty
            ric = CaptialTicker.replace(" COMDTY", "")
        if CaptialTicker.endswith(" JT EQUITY"): # e.g. HK-KIOX-125L, 
            ric = CaptialTicker.replace(" JT EQUITY", ".T")
        return ric

    def _GetDisplayHoldings(self, table: str, holdings_copy: pd.DataFrame) -> pd.DataFrame:
        df_display_holding = None
        if table == 't_pcf_holding':
            pattern_table = self.PcfDisplayComponent_PatternMap_
        elif table == 't_iopvpcf_holding':
            pattern_table = self.IOPVPcfDisplayComponent_PatternMap_
        else:
            raise Exception(f"_GetDisplayHoldings: Unknown table: {table}")

        if self.tradar_code_ in pattern_table:
            pattern = pattern_table[self.tradar_code_]
            # now traverse holdings_copy and if the ticker matches the pattern, save the index. if the matches > 1, throw exception.
            # at the same time, save the index for non-matching tickers.

            matching_indices = []
            non_matching_indices = []
            for idx, ticker in enumerate(holdings_copy['Ticker']):
                if re.search(pattern, ticker.upper()): # remember to upper, so that case-insensitive
                    matching_indices.append(idx)
                else:
                    non_matching_indices.append(idx)

            if len(matching_indices) > 1:
                raise Exception(f"We expected only one display holdings for {self.tradar_code_} {table} now. But may have multiple display holding, need design how to distribute among them.")

            display_idx = matching_indices[0]
            display_holding = holdings_copy.loc[display_idx]

            disp_hld_price = display_holding['Price']
            disp_hld_shares = display_holding['Shares_Per_Basket']
            disp_hld_contract_size = display_holding['Val_Point']
            disp_hld_lc_per_fc = display_holding['FX_LC_FC']

            disp_hld_bbgticker = display_holding['BbgTicker']
            logger.info(f"Display holding {disp_hld_bbgticker} shares = {disp_hld_shares}. ")

            # now for each non-matching index, we calculate the equivalent shares in matching index
            for idx in non_matching_indices:
                price = holdings_copy.at[idx, 'Price']
                shares = holdings_copy.at[idx, 'Shares_Per_Basket']
                contract_size = holdings_copy.at[idx, 'Val_Point']
                lc_per_fc = holdings_copy.at[idx, 'FX_LC_FC'] # Fx LC / FC

                bbgticker = holdings_copy.at[idx, 'BbgTicker']

                value_fc = price * shares * contract_size / lc_per_fc
                # value_fc = disp_hld_price * disp_hld_shares * disp_hld_contract_size / disp_hld_lc_per_fc
                equivalent_shares = value_fc / (disp_hld_price * disp_hld_contract_size / disp_hld_lc_per_fc)

                logger.info(f"Non-display holding {bbgticker}, equivalent shares = {equivalent_shares}. ")

                disp_hld_shares = disp_hld_shares + equivalent_shares
            
            holdings_copy.loc[display_idx, 'Shares_Per_Basket'] = disp_hld_shares
            logger.info(f"Display holding {disp_hld_bbgticker} final shares = {disp_hld_shares}. ")

            # now keep only the display holding in the dataframe
            df_display_holding = holdings_copy.loc[[display_idx]]
        else:
            df_display_holding = holdings_copy

        return df_display_holding

    def _SyncPcfComponents(self, riskDbClnt, opsDbClnt, pcfGenDbClnt, as_of_date: datetime) -> bool:
        
        with riskDbClnt.cursor() as cursor:
            holdingid_sql = f"select acct_holding_id from account_holdings where acct_book_id = '{self.cdb_fund_id_}' and as_of_date = '{as_of_date.strftime(g_riskdb_dateformat)}' and holding_type = '{self.RECON_TYPE}' and is_latest = 1"
            logger.info(f"_SyncPcfComponents {self.tradar_code_} holdingid sql:\n {holdingid_sql}")

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

            sql2 = f"select ticker, isin, amount, price, ccy, fx_lc_fc, security_desc, fund_ccy, classification, security_type, exposure_nav_percent, val_point from account_holdings_data where acct_holding_id = '{acct_holding_id}' and classification in ({classification_str})"
            logger.info(f"_SyncPcfComponents {self.tradar_code_} holding sql:\n {sql2}")

            cursor.execute(sql2)
            rows = cursor.fetchall()

            # save the result in a pandas dataframe for later use in PCF generation.
            holdings = pd.DataFrame(rows, columns=['Ticker', 'ISIN', 'Amount', 'Price', 'CCY', 'FX_LC_FC', 'Security_Desc', 'Fund_CCY', 'Classification', 'Security_Type', 'Exposure_NAV_Percent', 'Val_Point'])
            holdings[['Amount', 'Price', 'FX_LC_FC', 'Exposure_NAV_Percent', 'Val_Point']] = holdings[['Amount', 'Price', 'FX_LC_FC', 'Exposure_NAV_Percent', 'Val_Point']].astype(float)
            # ticker = "SOBRKB2L Index"

            if holdings.empty:
                logger.info(f"_SyncPcfComponent Sql2: {sql2}")
                logger.error(f"No holding with classification '{self.holding_classification_}' for fund {self.tradar_code_} as of date {as_of_date.strftime(g_db_dateformat)}.")
                return False

            # copy dataframe holdings to self.expo2_holdings_ for later use in iopv share calculation. Because in 7709, the price will be adjusted by swapbasis. However, we need the row price for iopv share calculation.
            self.expo2_holdings_ = holdings.copy()

            self.holdings_ = self._MergeHolding(holdings)

            self.fund_ccy_ = self.holdings_["Fund_CCY"].iloc[0] if not self.holdings_.empty else None # fund_ccy should be same for all rows.

            if self.tradar_code_ in self.EquityFut_SwapBasis_ + self.EquityFutSwap_Option_:
                sqlswapbasis = f"select value_lc from account_holdings_data where acct_holding_id = '{acct_holding_id}' and classification = 'Payable/Receivable' and cash_class like '{self.SL_key_}'"
                cursor.execute(sqlswapbasis)
                rows_swapbasis = cursor.fetchall()
                self.swapbasis_total_ = float(sum(row[0] for row in rows_swapbasis) if rows_swapbasis else 0.0)

        with opsDbClnt.cursor() as cursor:
            sql_total_fund_units= f"select pma_units from fund_level_data where acct_book_id = '{self.cdb_fund_id_}' and acct_book_class_id = '{self.cdb_listed_class_id_}' and recon_type = '{self.RECON_TYPE}' and as_of_date = '{as_of_date.strftime(g_opsdb_dateformat)}'"

            cursor.execute(sql_total_fund_units)
            row = cursor.fetchone()
            if row is None:
                logger.info(f"_SyncPcfComponent Sql_total_fund_units: {sql_total_fund_units}")
                logger.error(f"No fund level data found for fund {self.tradar_code_} {self.cdb_fund_id_} as of date {as_of_date.strftime(g_db_dateformat)}.")
                return False
            
            fund_units = float(row[0])

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
        self.holdings_["Isin"] = ''
        with pcfGenDbClnt.cursor() as cursor:
            for ticker in self.holdings_["Ticker"]:
                # get the Classification from self.holdings_ for this ticker
                classification = self.holdings_.loc[self.holdings_["Ticker"] == ticker, "Classification"].values[0]

                if classification in (self.CLASSIFICATION_SWAP, self.CLASSIFICATION_FUTURES):                
                    sql3 = f"select RIC, BbgTicker, Isin from t_ticker_bbgric where HoldingTicker = '{ticker}'"
                    cursor.execute(sql3)
                    row = cursor.fetchone() # ticker is the primary key, so only one row is expected.
                    if row is not None:
                        self.holdings_.loc[self.holdings_["Ticker"] == ticker, "RIC"] = row[0]
                        self.holdings_.loc[self.holdings_["Ticker"] == ticker, "BbgTicker"] = row[1]
                        self.holdings_.loc[self.holdings_["Ticker"] == ticker, "Isin"] = row[2]
                    else:
                        # try to derive RIC from BbgTicker if possible
                        ric = self._DerivedRic(ticker)
                        if ric is not None:
                            # when we derive the RIC, cannot derive ISIN, need to get the ISIN from the holdings
                            holding_isin = self.holdings_.loc[self.holdings_["Ticker"] == ticker, "ISIN"]
                            self.holdings_.loc[self.holdings_["Ticker"] == ticker, "RIC"] = ric
                            self.holdings_.loc[self.holdings_["Ticker"] == ticker, "BbgTicker"] = ticker.upper() # just to match the t_ticker_bbgric
                            self.holdings_.loc[self.holdings_["Ticker"] == ticker, "Isin"] = holding_isin
                        else:
                            logger.info(f"_SyncPcfComponent Sql3: {sql3}")
                            errmsg = f"No RIC and Bloomberg ticker found for {ticker} in t_ticker_bbgric. PCF generation for {self.tradar_code_} failed."
                            raise Exception(errmsg)
                # handle options
                elif classification == self.CLASSIFICATION_INVESTMENTS:
                    if re.match(r"^000660 KS \d{2}/\d{2}/\d{2} [CP]\d+ EQUITY$", ticker, re.IGNORECASE):
                        self.holdings_.loc[self.holdings_["Ticker"] == ticker, "BbgTicker"] = ticker
                        self.holdings_.loc[self.holdings_["Ticker"] == ticker, "RIC"] = ticker.replace("EQUITY", "").strip()
                        self.holdings_.loc[self.holdings_["Ticker"] == ticker, "Isin"] = ''
                else:
                    logger.error(f"_SyncPcfComponent Unknown '{classification}' '{ticker}' '{self.tradar_code_}'. PCF generation failed.")
                    raise Exception(f"_SyncPcfComponent Unknown '{classification}' '{ticker}' '{self.tradar_code_}'. PCF generation failed.")
                
        # now insert data to pcfGenDbClnt table t_pcf_holding.
        # TradarCode = self.tradar_code_
        # Ticker = self.holdings_["Ticker"] but substring from begin to the first space.
        # MarketSector = self.holdings_["Ticker"] substring from the first space to the end.
        # Share = self.holdings_["Amount"]
        # Price = self.holdings_["Price"]
        # Ccy = self.holdings_["CCY"]
        # FxRate = self.holdings_["FX_LC_FC"]
        # SecurityDesc = self.holdings_["Security_Desc"]
        # AsOfDate = as_of_date in g_db_dateformat

        as_of_date_str = as_of_date.strftime(g_db_dateformat)

        listShareWeight = self.ComponentListWeight(riskDbClnt, opsDbClnt, as_of_date.strftime(g_opsdb_dateformat)) # this is the weight of the listed share class in the fund, used to calculate the amount of each component in the listed share class.
        calcshares = self._CalcShares(fund_units)

        logger.info(f"SyncPcfComponents: {self.tradar_code_}, {as_of_date_str}, calcshares={calcshares}, listShareWeight={listShareWeight}")

        # Below line is on panda, so it calculates for every ticker (or for loop in tranditional code), add "Shares_Per_Basket" columne to holdings_ dataframe.
        logger.info(f"Calculating Shares_Per_Basket = Amount * listShareWeight * basket_size / calcshares with listShareWeight={listShareWeight}, basket_size={self.basket_size_}, totalshares={calcshares}")

        self.holdings_['Shares_Per_Basket'] = (
            self.holdings_['Amount'] * listShareWeight * self.basket_size_ / calcshares
        ).round(4)

        logger.info(f"Result of Shares_Per_Basket calculation:\n{self.holdings_[['Ticker', 'Amount', 'Shares_Per_Basket']]}")

        if self.tradar_code_ in self.EquityFut_SwapBasis_ + self.EquityFutSwap_Option_:
            # adjust the price for each row
            for index, row in self.holdings_.iterrows():
                # Since HK-SKHY-2L, holdings add Option. For Option, we don't need to adjust the price. Only for swap need to add swapbasis
                if row['Classification'] == self.CLASSIFICATION_SWAP:
                    contract_size = row['Val_Point']
                    self.holdings_.loc[index, 'Price'] = row['Price'] + (self.swapbasis_total_ / contract_size / row['Amount'])
                    logger.info(f"Adjust price for {self.tradar_code_} price={row['Price']}, swapbasis_total={self.swapbasis_total_}, contract_size={contract_size}, N_SHARES={row['Amount']}, adjusted price={self.holdings_.loc[index, 'Price']}")
        try:
            # before insert, delete existing records for the same TradarCode and AsOfDate to avoid duplicate records.
            for table in ["t_pcf_holding", "t_iopvpcf_holding"]:
                  with pcfGenDbClnt.cursor() as cursor:
                    cursor.execute(f"DELETE FROM {table} WHERE TradarCode = '{self.tradar_code_}' AND AsOfDate = '{as_of_date_str}'")

                    holdings_copy = self.holdings_.copy()

                    logger.info(f"Pass to _GetDisplayHoldings holdings_copy:\n{holdings_copy}")
                    display_holdings = self._GetDisplayHoldings(table, holdings_copy)

                    for index, row in display_holdings.iterrows():
                        ticker = row['Ticker']

                        security_type = row['Security_Type']
                        if table == "t_iopvpcf_holding" and security_type == 'Equity Call':
                            logger.info(f"Skip Equity Call ({ticker}) in {table} for {self.tradar_code_} as of date {as_of_date_str}.")
                            continue
                        
                        shares_per_basket = row['Shares_Per_Basket'] # [(amt of this component in this fund)/total shares]*basket_size = shares per basket
                        if table == "t_pcf_holding" and self.tradar_code_ in ['HK-KIOX-125L']:
                            # convert shares_per_basket to the cloest 100 shares
                            shares_per_basket = round(shares_per_basket / 100) * 100

                        logger.info(f"SyncPcfComponents: table={table}, tradar_code={self.tradar_code_}, ticker={ticker}, share_per_basket={shares_per_basket}, basket_size={self.basket_size_}, calcshares={calcshares}")

                        cursor.execute(f"""
                            INSERT INTO {table} (TradarCode, Ticker, Share, Price, Ccy, FxRate, Description, AsOfDate, Coupon, ValPoint)
                            VALUES ('{self.tradar_code_}', '{ticker}', {shares_per_basket}, {row['Price']}, '{row['CCY']}', {row['FX_LC_FC']}, '{row['Security_Desc']}', '{as_of_date_str}', 0, {row['Val_Point']})
                        """)
            pcfGenDbClnt.commit()
        except Exception as e:
            logger.exception(f"SyncPcfComponents persist {table} for fund {self.tradar_code_} as of date {as_of_date_str}: {e}")
            return False
        
        return True

    def _CalcShares(self, total_units: float) -> float:
        # for lnI, calcshares = total_units. But for other fund, it can be different. So put the logic in different function for easy override.
        return total_units
    
    def _SyncPcfFundData(self, riskDbClnt, opsDbClnt, pcfGenDbClnt, as_of_date: datetime) -> bool:

        # Sync components first, then sync fund level data. Because fund level data need count of holdings.
        if self._SyncPcfComponents(riskDbClnt, opsDbClnt, pcfGenDbClnt, as_of_date):
            logger.info(f"SyncPcfComponents succeeded. Now sync Pcf fund level data.") 

            ops_table = "fund_level_data"
            sql_nav2 = f"""select as_of_date, recon_type, acct_book_id as fund_id, acct_book_class_id as class_id, 
                      pma_nav as nav, pma_nav_per_share as nav_per_share, pma_units as units from {ops_table}
                      where recon_type = '{self.RECON_TYPE}' and as_of_date = '{as_of_date.strftime(g_riskdb_dateformat)}' and is_nav_holiday = 0 and acct_book_id = '{self.cdb_fund_id_}' and acct_book_class_id = '{self.cdb_listed_class_id_}'
                      """

            with opsDbClnt.cursor() as cursor:
                cursor.execute(sql_nav2)
                row = cursor.fetchone()
                if row is None:
                    logger.info(f"Sql: {sql_nav2}")
                    logger.error(f"No fund level data found for fund {self.tradar_code_} {self.cdb_fund_id_} as of date {as_of_date.strftime(g_db_dateformat)}.")
                    return False
                
                nav2 = float(row[4])
                self.nav2_per_share_ = float(row[5])
                units2 = float(row[6])

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
                fee_rate = float(row[1])
                fee_fixed = float(row[2])
            
            # calculate the estimated cash
            
            # day_diff = difference in day between as_of_date and pcf file date
            pcf_file_date_dt = datetime.strptime(self.pcf_file_date_, g_db_dateformat)
            day_diff = (pcf_file_date_dt - as_of_date).days

            # calculate the component value. self.holdings_ has columns e.g. ["Ticker", "ISIN", "Amount", "Price", "CCY", "FX_LC_FC", "Security_Desc", "Contract_Size", "shares_per_basket"]
            # component value = sum(shares_per_basket * Price * FxRate * Contract_Size). Traverse all rows in self.holdings_ to calculate the sum.
            
            fundshares = self._CalcShares(units2) # for different funds, this can be different, here not difference using units1 or units2, they should be same

            component_value = 0.0
            for index, row in self.holdings_.iterrows():
                component_share_per_basket = row['Shares_Per_Basket'] # this is weighted for listed shared class
                component_price = round(row['Price'], 4)
                component_fx = row['FX_LC_FC']
                component_contract_size = row['Val_Point']
                component_value += (component_share_per_basket * component_price * component_contract_size)/component_fx
            
            component_value = round(component_value, 4)

            if self.tradar_code_ == "HK-KIOX-125L":
                round_component_value = 0.0
                for index, row in self.holdings_.iterrows():
                    component_share_per_basket = row['Shares_Per_Basket'] # this is weighted for listed shared class
                    round_component_share_per_basket = round(component_share_per_basket / 100) * 100
                    component_price = round(row['Price'], 4)
                    component_fx = row['FX_LC_FC']
                    component_contract_size = row['Val_Point']
                    round_component_value += (round_component_share_per_basket * component_price * component_contract_size)/component_fx
                
                round_component_value = round(round_component_value, 4)

            nav2 = round(nav2, 2)
            #festimatecash=round((@fnetassetvalue*(1-@ffeerate/365*@fdaydiff)-@ffeefixed*@fdaydiff)/@fcalcshares*@fbasketsize-@fcomponentsvalue,4)
            if self.tradar_code_ in (self.IndexFut_Swap_ + self.IndexFut_ + self.EquityFut_SwapBasis_ + self.EquityFutSwap_Option_ + self.EquityCFD_):
                if self.tradar_code_ == "HK-KIOX-125L":
                    rounding = 2
                else:
                    rounding = 4

                logger.info(f"{self.tradar_code_} pcf & iopv estimated_cash calculation: round(nav2 / fundshares, {rounding})*basket_size - component_value; iopv may override!")
                
                # here nav2 and fundshares are counted for listed share class only, component_value is weighted for listed share class as well.
                estimated_cash = round(nav2 / fundshares, rounding)*basket_size - component_value # my understanding: follow this formula for equity base ETF
                logger.info(f"{self.tradar_code_} nav2={nav2}, fundshares={fundshares}, basket_size={self.basket_size_}, component_value={component_value}, estimated_cash={estimated_cash}")
                if self.tradar_code_ == "HK-KIOX-125L":
                    round_estimated_cash = round(nav2 / fundshares, rounding)*basket_size - round_component_value
                    logger.info(f"{self.tradar_code_} using rounded 100 shares: nav2={nav2}, fundshares={fundshares}, basket_size={self.basket_size_}, round_component_value={round_component_value}, round_estimated_cash={round_estimated_cash}")
            else:
                logger.info(f"calc estimated_cash of {self.tradar_code_}: round(((nav2 * (1 - fee_rate / 365 * day_diff) - fee_fixed * day_diff) / fundshares) * basket_size - component_value, 4); iopv may override!")
                
                estimated_cash = round(((nav2 * (1 - fee_rate / 365 * day_diff) - fee_fixed * day_diff) / fundshares) * basket_size - component_value, 4)
                logger.info(f"{self.tradar_code_} nav2={nav2}, fee_rate={fee_rate}, day_diff={day_diff}, fee_fixed={fee_fixed}, fundshares={fundshares}, basket_size={self.basket_size_}, component_value={component_value}, estimated_cash={estimated_cash}")
        else:
            logger.error(f"SyncPcfFundData: SyncPcfComponents failed. Sync pcf components then sync fund level data")
            return False
        
        as_of_date_str = as_of_date.strftime(g_db_dateformat)
        try:

            for table in ["t_pcf_fund", "t_iopvpcf_fund"]:
                with pcfGenDbClnt.cursor() as cursor:
                    # Note: EquityFutSwap_Option_ (7709) iopv pcf is not included below
                    if table == "t_iopvpcf_fund":
                        if self.tradar_code_ in (self.IndexFut_Swap_ + self.IndexFut_ + self.EquityFut_SwapBasis_ + self.EquityFutSwap_Option_):
                            # if self.tradar_code_ in ('HK-SMSN-2L') or self._HoldingEquityCall(): # HK-SMSN-2L and HK-SKHY-2L for now, using NAV2 per share
                            #     estimated_cash = self.nav2_per_share_ * basket_size
                            #     logger.info(f"{self.tradar_code_} iopv estimated_cash: nav2_per_share_ * basket_size = {self.nav2_per_share_} * {basket_size} = {estimated_cash}")
                            # else:
                            #     estimated_cash = nav1_per_share * basket_size
                            #     logger.info(f"{self.tradar_code_} iopv estimated_cash: nav_per_share1 * basket_size = {nav1_per_share} * {basket_size} = {estimated_cash}")
            
                            # Per tony liang's suggestion, all etfs are going to use nav1_per_share
                            estimated_cash = nav1_per_share * basket_size
                            logger.info(f"{self.tradar_code_} iopv estimated_cash override: nav_per_share1 * basket_size = {nav1_per_share} * {basket_size} = {estimated_cash}")

                    # before insert, delete existing records for the same TradarCode and AsOfDate to avoid duplicate records.
                    cursor.execute(f"DELETE FROM {table} WHERE TradarCode = '{self.tradar_code_}' AND AsOfDate = '{as_of_date_str}'")

                    if table == "t_pcf_fund" and self.tradar_code_ in ["HK-KIOX-125L"]:
                        cursor.execute(f"""
                            INSERT INTO {table} (TradarCode, NavPerShare, EstimateCash, TotalShares, AsOfDate, FundCcy)
                            VALUES ('{self.tradar_code_}', {nav1_per_share}, {round_estimated_cash}, {fundshares}, '{as_of_date_str}', '{self.fund_ccy_}')
                        """)
                    else:
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
                # self.iopv_estimated_cash_ = row[1]
                # if self._HoldingEquityCall():
                #     estimated_cash_raw = self.nav2_per_share_ * self.basket_size_ # for fund with equity call option, use nav2_per_share * basket_size as estimated cash
                # else:
                #     estimated_cash_raw = self.iopv_estimated_cash_ # self.iopv_estimated_cash_ is not used currently, but keep it for future use
                
                self.iopv_estimated_cash_ = row[1]
                estimated_cash_raw = self.iopv_estimated_cash_
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
                    price = self._formatFloatAsInt(float(round(row[2],4))) # changed to 4 because of HK-MSTR-2L found 4 digits on price

                    ccy = row[3]
                    description = row[4]

                    isin = self.holdings_.loc[self.holdings_['Ticker'] == ticker, 'Isin'].values[0]
                    if isin is None:
                        isin = '' # instead of write "none" in the pcf, write an empty string
                    ric = self.holdings_.loc[self.holdings_['Ticker'] == ticker, 'RIC'].values[0]
                    bbgticker = self.holdings_.loc[self.holdings_['Ticker'] == ticker, 'BbgTicker'].values[0]
                    contract_size_raw = self.holdings_.loc[self.holdings_['Ticker'] == ticker, 'Val_Point'].values[0]
                    contract_size = self._formatFloatAsInt(contract_size_raw) # convert 1.0 to 1, but if 1.23, keep 1.23
                    
                    f.write(f"{index},{description},{ccy},{price},{share},{isin},,{ric},{contract_size},{bbgticker}\n")
                    index += 1

    def _CalcEquityCallIopvShare(self, ticker: str) -> float:
        nav2_per_share = self.nav2_per_share_
        basket_size = self.basket_size_
        nav2_per_basket = nav2_per_share * basket_size

        Equity_Fut_Swap_Exp = self.holdings_.loc[
            self.holdings_['Security_Type'] == "Equity Fut Swap",
            "Exposure_NAV_Percent",
        ].sum()
        Equity_Call_Exp = self.holdings_.loc[
            self.holdings_['Security_Type'] == "Equity Call",
            "Exposure_NAV_Percent",
        ].sum()
        Equity_Hedge_Fund_Exp = self.holdings_.loc[
            self.holdings_['Security_Type'] == "Equity Hedge Fund",
            "Exposure_NAV_Percent",
        ].sum()

        total_nav_exp = (Equity_Fut_Swap_Exp + Equity_Call_Exp + Equity_Hedge_Fund_Exp) / 100.0 # convert % to float

        # current case of 7709, iopv pcf only one row for Equity Fut Swap, but fx_lc_fs is assumed same all rows.
        fx_lc_fc = self.holdings_.loc[self.holdings_['Ticker'] == ticker, 'FX_LC_FC'].values[0]

        ticker_price = self.expo2_holdings_.loc[self.expo2_holdings_['Ticker'] == ticker, 'Price'].values[0]
        ticker_contract_size = self.holdings_.loc[self.holdings_['Ticker'] == ticker, 'Val_Point'].values[0]

        iopv_share = round(
            (nav2_per_basket * total_nav_exp) / (ticker_price * ticker_contract_size / fx_lc_fc),
            4,
        )

        logger.info(f"CalcEquityCallIopvShare {self.tradar_code_} calc shares: {ticker}, nav2/basket={nav2_per_basket}, total_exp={total_nav_exp}, price={ticker_price}, contract_size={ticker_contract_size}, fx_lc_fc={fx_lc_fc}, result iopv_share={iopv_share}")

        return iopv_share
    
    def _WriteIopvPcf(self, filename: str, filedate: str, pcfGenDbClnt, as_of_date_str) -> None:
        with open(filename, "w") as f:
            self._WritePcfHeader(f, filedate, True, pcfGenDbClnt, as_of_date_str)

            f.write(",Company Name,Currency,Price,Shares,ISIN,SEDOL,RIC,Contract Size\n")
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

                    isin = self.holdings_.loc[self.holdings_['Ticker'] == ticker, 'Isin'].values[0]
                    if isin is None:
                        isin = '' # instead of write "none" in the pcf, write an empty string
                    ric = self.holdings_.loc[self.holdings_['Ticker'] == ticker, 'RIC'].values[0]

                    if self._HoldingEquityCall():
                        share = self._CalcEquityCallIopvShare(ticker)
                        
                    if self.tradar_code_ in (self.IndexFut_Swap_ + self.IndexFut_ + self.EquityFut_SwapBasis_ + self.EquityFutSwap_Option_ + self.EquityCFD_): # special for these funds, need contract size in IOPV PCF, normally don't need
                        contract_size_raw = self.holdings_.loc[self.holdings_['Ticker'] == ticker, 'Val_Point'].values[0]
                        contract_size = self._formatFloatAsInt(contract_size_raw)
                        f.write(f"{index},{description},{ccy},{price},{share},{isin},,{ric},{contract_size}\n")
                    else:    
                        f.write(f"{index},{description},{ccy},{price},{share},{isin},,{ric},\n") # exclude the contract size column for IOPV PCF

                    index += 1

    def _mask_ticker(self, ticker: str) -> str:
        # Mask detail ticker in hsbc pcf file
        # Replace pattern HCTQ6 with HSTECH, HCT is fixed, Q6 will be changed, but keep in a letter + a digit format.
        return re.sub(r'^HCT[A-Z]\d(?=$|\s)', 'HSTECH', ticker)

    def _WriteHsbcPcf(self, filename: str, filedate: str, pcfGenDbClnt, as_of_date_str) -> None:

        with open(filename, "w") as f:
            holding_rows = []
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
                    # for HK-SKHY-2L 7709, hsbc file follows t_iopvpcf_holding. However, HK-GLD-L 7299, hsbc file follows PCF. In fact all hsbc file default should follow PCF.
                    table_name = 't_pcf_holding'
                    if self.tradar_code_ == 'HK-SKHY-2L':
                        table_name = 't_iopvpcf_holding'
                        
                    sql_holding = f"select Ticker, Share, Price, Ccy, Description from {table_name} where TradarCode = '{self.tradar_code_}' and AsOfDate = '{as_of_date_str}'"

                    with pcfGenDbClnt.cursor() as cursor:
                        cursor.execute(sql_holding)
                        holding_rows = cursor.fetchall()

                    num_of_components = len(holding_rows)
                    f.write(f'{line_index},{num_of_components}\n')

                if line_index == 3:
                    estimated_cash = round(self.estimated_cash_, 2)

                    if self._HoldingEquityCall(): # for now, only 7709
                        nav2_per_share = self.nav2_per_share_
                        basket_size = self.basket_size_
                        nav2_per_basket = nav2_per_share * basket_size

                        estimated_cash = 0.0
                        # for 7709, only one row   
                        for row in holding_rows:
                            component_ticker = row[0]
                            iopv_share = self._CalcEquityCallIopvShare(component_ticker)
                            fx_lc_fc = self.holdings_.loc[self.holdings_['Ticker'] == component_ticker, 'FX_LC_FC'].values[0]

                            ticker_price = float(row[2]) # use the price in iopvpcf, it is adjusted by swapbasis. convert from Decimal to float
                            ticker_contract_size = self.holdings_.loc[self.holdings_['Ticker'] == component_ticker, 'Val_Point'].values[0]

                            estimated_cash_ticker = nav2_per_basket - (ticker_price * iopv_share * ticker_contract_size) / fx_lc_fc
                            logger.info(f"_WriteHsbcPcf est_cash {self.tradar_code_} {component_ticker}: nav2_per_basket={nav2_per_basket}, price={ticker_price}, share={iopv_share}, contract_size={ticker_contract_size}, fx_lc_fc={fx_lc_fc}, estimated_cash={estimated_cash_ticker}")
                            estimated_cash += estimated_cash_ticker

                        estimated_cash = round(estimated_cash, 3)

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
                
                if line_index == 7: # write hsbc shares for each component
                    # reuse the holding_rows fetched in line_index == 2
                    for row in holding_rows:
                        component_ticker = row[0]
                        share = self._formatFloatAsInt(float(row[1]))
                        if self._HoldingEquityCall():
                            share = self._CalcEquityCallIopvShare(component_ticker)

                        contract_size = self.holdings_.loc[self.holdings_['Ticker'] == component_ticker, 'Val_Point'].values[0]
                        hsbc_share = round(share * contract_size, 5)
                        comp_ticker = component_ticker.split(" ", 1)[0]
                        if '=' in comp_ticker:
                            comp_ticker = comp_ticker.split('=')[0] # e.g. 00593=Q6 -> 00593, to be compatible with current version
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

                    isin = self.holdings_.loc[self.holdings_['Ticker'] == ticker, 'Isin'].values[0]
                    if isin is None:
                        isin = '' # instead of write "none" in the pcf, write an empty string

                    ric = self.holdings_.loc[self.holdings_['Ticker'] == ticker, 'RIC'].values[0]
                    masked_ric = self._mask_ticker(ric)
                    bbgticker = self.holdings_.loc[self.holdings_['Ticker'] == ticker, 'BbgTicker'].values[0]
                    masked_bbgticker = self._mask_ticker(bbgticker)

                    contract_size_raw = self.holdings_.loc[self.holdings_['Ticker'] == ticker, 'Val_Point'].values[0]
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
       
        pcf_status_to, nav2_timestamp_dt = self._CheckNav2Status(opsDbClnt, pcfGenDbClnt)
        
        if pcf_status_to == PCF_STATUS.Pending and nav2_timestamp_dt is None:
            return pcf_status_to # no signoff case
        
        if pcf_status_to == PCF_STATUS.No:
            if force == False:
                return pcf_status_to # nothing changed
            else:
                self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav2_timestamp_dt)
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

        self._UpdatePcfStatus(pcfGenDbClnt, pcf_status_to, nav2_timestamp_dt, as_of_date_str) # inside is insert a new line

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


    def _CheckNav2Status(self, opsDbClnt, pcfGenDbClnt):

        try:
            # expected_nav_date_ based on t_nav_holiday find T-1 day. Don't check is_nav_holiday in fund_level_data.
            # using fund_level_data is not reliable. In case of custodian delay, there is not entry in the db. Then we cannot know it is a holiday or not. 
            asofdate = self.expected_nav_date_.strftime(g_opsdb_dateformat)
            sql = f"select version_date from fund_level_data where acct_book_id = '{self.cdb_fund_id_}' and acct_book_class_id = '{self.cdb_listed_class_id_}' and recon_type = '{self.RECON_TYPE}' and as_of_date = '{asofdate}'"
            with opsDbClnt.cursor() as cursor:
                cursor.execute(sql)
                row = cursor.fetchone()
                if row is None:
                    nav2_version_date = None
                    nav2_version_date_dt = None
                else:
                    nav2_version_date = row[0]
                    nav2_version_date_std = self._Nav2TimestampStdFormat(nav2_version_date)
                    nav2_version_date_dt = datetime.strptime(nav2_version_date_std, g_opsdb_dateformat + " %H:%M:%S")

            # to find the latest status and Nav2TimeStamp, use Id desc not the timestamp
            sql2 = f"select Top 1 Nav2TimeStamp, Status from t_pcf_status where TradarCode = '{self.tradar_code_}' and FileDate = '{self.pcf_file_date_}' order by Id desc"
            
            with pcfGenDbClnt.cursor() as cursor:
                cursor.execute(sql2)
                row = cursor.fetchone()
                
                # from the sql, it is in datetime type
                nav2_timestamp_dt = row[0] if row else None
                pcf_status = row[1] if row else None

            # if not force case, will continue below
            if nav2_version_date_dt is None:
                logger.warning(f"_CheckNav2Status:{self.tradar_code_} as of nav date {asofdate} is not signoff")
                logger.info(f"check Signoff Sql: {sql}")
                going_to_status = PCF_STATUS.Pending
                nav2_timestamp_dt = None

                if pcf_status is None:
                    logger.info(f"_CheckNav2Status: first check {self.tradar_code_}, nav date {asofdate}, insert new record")
                    self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav2_timestamp_dt)
                elif pcf_status == PCF_STATUS.Done.value:
                    logger.info(f"_CheckNav2Status: {self.tradar_code_}, nav date {asofdate} is unsignoff")
                    self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav2_timestamp_dt)
            else:
                # now need to check whether revise or first time generate PCF.            
                if pcf_status is None:
                    logger.info(f"_CheckNav2Status: first check {self.tradar_code_}, nav date {asofdate}, insert new record")
                    going_to_status = PCF_STATUS.Done
                    nav2_timestamp_dt = nav2_version_date_dt
                    self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav2_timestamp_dt)
                elif pcf_status == PCF_STATUS.Pending.value:
                    going_to_status = PCF_STATUS.Done
                    nav2_timestamp_dt = nav2_version_date_dt
                    # we don't insert another pending status
                else:
                    # here pcf_Status should be Done
                    if nav2_version_date_dt > nav2_timestamp_dt:
                        going_to_status = PCF_STATUS.Revise 
                        nav2_timestamp_dt = nav2_version_date_dt   
                        self._newPcfStatus(pcfGenDbClnt, self.tradar_code_, PCF_STATUS.Pending, nav2_timestamp_dt)
                    else:
                        # NAV2 is not changed.
                        going_to_status = PCF_STATUS.No
                        nav2_timestamp_dt = nav2_version_date_dt

        except Exception as e:
            logger.exception(f"CheckNav2Status ex: {e}")
            logger.info(f"Sql: {sql}")

        return going_to_status, nav2_timestamp_dt
    
    def _Nav2TimestampStdFormat(self, nav2_timestamp) -> str:
        if isinstance(nav2_timestamp, str):
            ts = nav2_timestamp.strip().replace("T", " ")
            if "+" in ts:
                ts = ts.split("+", 1)[0].strip()

            if "." in ts:
                ts = ts.split(".", 1)[0].strip()

            nav2_timestamp = ts

        # logger.info(f"_Nav2TimestampStdFormat: nav2_timestamp={nav2_timestamp}")
        return nav2_timestamp
    
    def _UpdatePcfStatus(self, pcfGenDbClnt, status, nav2_timestamp_dt, as_of_date_str) -> None:
        
        try:
            if status != PCF_STATUS.No:
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
                        (self.tradar_code_, self.pcf_file_date_, status.value, now, nav2_timestamp, as_of_date_str),
                    )
                    pcfGenDbClnt.commit()
                        
        except Exception as e:
            logger.exception(f"_UpdatePcfStatus: {e}")
            logger.info(f"Sql: {insert_sql}")
        
