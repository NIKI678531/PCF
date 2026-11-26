import csv
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pandas as pd


logger = logging.getLogger('Main')


class DividendFileReader:
    COLUMN_MAPPING = {
        "Lcal Cde": "Ticker",
        "Exch": "MIC",
        "XD Dt": "ExDate",
        "Lcal Ccy": "DividendCcy",
        "Div_Grs": "GrossDividend",
        "Idx Cde": "IndexCode"
    }

    # columns of t_indx_dividend
    INSERT_COLUMNS = (
        "Ticker",
        "MIC",
        "ExDate",
        "DividendCcy",
        "GrossDividend",
        "UpdatedOn",
        "IndexCode",
        "IndexSource",
    )

    NUMERIC_COLUMNS = {
        "GrossDividend"
    }

    def __init__(self, pcfGenDbClnt, table_name):
        self.pcfGenDbClnt_ = pcfGenDbClnt
        self.table_name_ = table_name

    def _BuildRecord(self, csv_row, row_number, MIC, updated_on, index_source):
        record = {
            "MIC": MIC,
            "UpdatedOn": updated_on,
            "IndexSource": index_source,
        }

        for csv_column, tbl_column in self.COLUMN_MAPPING.items():
            raw_value = csv_row.get(csv_column)

            if tbl_column in self.NUMERIC_COLUMNS:
                record[tbl_column] = self._ParseDecimal(raw_value, csv_column, row_number)
            else:
                if csv_column == 'Exch' and raw_value.strip() == "HK":
                    raw_value = "XHKG"
                record[tbl_column] = "" if raw_value is None else raw_value.strip()

        return record

    def ReadFile(self, dividend_file_fullpath, mic, index_source):
        """Import dividend rows for index_code without replacing other indexes."""
        df_records = pd.DataFrame(columns=self.INSERT_COLUMNS)
        updated_on = datetime.now()

        with open(dividend_file_fullpath, newline="", encoding="utf-8-sig") as dividend_file:
            reader = csv.DictReader(dividend_file)

            for row_number, csv_row in enumerate(reader, start=2):
                if self._IsBlankRow(csv_row):
                    continue

                df_records.loc[len(df_records)] = self._BuildRecord(csv_row, row_number, mic, updated_on, index_source)

        # for record in df_records aggregate GrossDividend for same Ticker and MIC and ExDate
        df_records = df_records.groupby(["Ticker", "MIC", "ExDate", "DividendCcy", "IndexCode"], as_index=False).agg({"GrossDividend": "sum"})
        df_records["UpdatedOn"] = updated_on

        if not df_records.empty:
            logger.info(f"DividendFileReader dividend records after merging\n%s", df_records.to_csv(sep=",", index=False, lineterminator="\n").rstrip())

        try:
            with self.pcfGenDbClnt_.cursor() as cursor:
                for _, record in df_records.iterrows():
                    ticker = record["Ticker"]
                    mic = record["MIC"]
                    ex_date = record["ExDate"]
                    updated_on = record["UpdatedOn"]
                    index_code = record["IndexCode"]

                    dividend_ccy = record["DividendCcy"]
                    gross_dividend = record["GrossDividend"]

                    # don't care whether exist, remove first always, using IndexSource to filter, remove all index code of this source
                    delete_sql = f"DELETE FROM {self.table_name_} WHERE Ticker = %s AND MIC = %s AND ExDate = %s AND IndexSource = %s AND IndexCode = %s"
                    cursor.execute(delete_sql, (ticker, mic, ex_date, index_source, index_code))

                    insert_sql = f"""
                        INSERT INTO {self.table_name_} ({', '.join(self.INSERT_COLUMNS)})
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """
                    cursor.execute(insert_sql, (ticker, mic, ex_date, dividend_ccy, gross_dividend, updated_on, index_code, index_source))

            self.pcfGenDbClnt_.commit()

        except Exception as e:
            self.pcfGenDbClnt_.rollback()
            err = f"DividendFileReader:Failed to persist {dividend_file_fullpath} into {self.table_name_}. ex={e}"
            logger.exception(err)
            raise RuntimeError(err) from e

        logger.info(f"Persisted {dividend_file_fullpath} into {self.table_name_}.")
        return len(df_records)

    def _ParseDecimal(self, value, column_name, row_number):
        try:
            decimal_value = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"Invalid numeric at {column_name} rows {row_number}: {value}") 

        return decimal_value

    def _IsBlankRow(self, csv_row):
        opn_date = csv_row.get("Opndate")
        local_code = csv_row.get("Lcal Cde")
        return (opn_date is None or str(opn_date).strip() == "") and (local_code is None or str(local_code).strip() == "")