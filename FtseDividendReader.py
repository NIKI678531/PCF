from DividendFileReader import DividendFileReader
from datetime import datetime
import logging
import pandas as pd
import csv

logger = logging.getLogger('Main')

class FtseDividendReader(DividendFileReader):

    # map dividend file columns to t_indx_dividend columns
    COLUMN_MAPPING = {
        "BBG Ticker": "Ticker",
        # "MIC": "MIC", # don't use MIC from index file, the code is different from our convention
        "Ex-Dividend Date": "ExDate",
        "ISO Currency Code": "DividendCcy",
        "Dividend Amount": "GrossDividend",
        "Index Marker": "IndexCode"
    }

    def __init__(self, pcfGenDbClnt, table_name):
        super().__init__(pcfGenDbClnt, table_name)

    def _BuildRecord(self, csv_row, row_number, MIC, updated_on, index_source):
        record = {
            "MIC": MIC,
            "UpdatedOn": updated_on,
            "IndexSource": index_source,
        }

        for csv_column, tbl_column in self.COLUMN_MAPPING.items():
            raw_value = csv_row.get(csv_column).strip()

            if tbl_column in self.NUMERIC_COLUMNS:
                record[tbl_column] = self._ParseDecimal(raw_value, csv_column, row_number)
            else:
                if csv_column == 'BBG Ticker':
                    record[tbl_column] = raw_value.split(" ")[0] # e.g. 005930 KP -> 005930
                elif csv_column == 'Ex-Dividend Date':
                    record[tbl_column] = datetime.strptime(raw_value, "%d/%m/%Y").strftime("%Y%m%d")
                else:
                    record[tbl_column] = raw_value

        return record

    def ReadFile(self, dividend_file_fullpath, mic, index_source):
        df_records = pd.DataFrame(columns=self.INSERT_COLUMNS)
        updated_on = datetime.now()

        with open(dividend_file_fullpath, newline="", encoding="utf-8-sig") as dividend_file:
            bfind_track03 = False
            for section_line_number, line in enumerate(dividend_file, start=1):
                if line.startswith("TRACK03 - Stock level data - Ex-dividend changes"):
                    bfind_track03 = True
                    break

            if not bfind_track03:
                err = f"FtseDividendReader: Missing TRACK03 section in {dividend_file_fullpath}"
                logger.error(err)
                raise ValueError(err)

            fieldnames = None
            for header_line_number, line in enumerate(dividend_file, start=section_line_number + 1):
                if line.startswith("Cons Code"):
                    fieldnames = next(csv.reader([line]))
                    break
                if "YYYYYYYY" in line.strip():
                    break
          
            if fieldnames is None:
                err = f"FtseDividendReader: Missing Cons Code header in TRACK03 in {dividend_file_fullpath}"
                logger.error(err)
                raise ValueError(err)

            reader = csv.DictReader(dividend_file, fieldnames=fieldnames)
            for csv_row in reader:
                if ("YYYYYYYY" in csv_row["Cons Code"]):
                    break

                row_number = header_line_number + reader.line_num
                df_records.loc[len(df_records)] = self._BuildRecord(csv_row, row_number, mic, updated_on, index_source)
            else:
                err = f"FtseDividendReader: Missing YYYYYYYY end marker in TRACK03 in {dividend_file_fullpath}"
                logger.error(err)
                raise ValueError(err)

        # for record in df_records aggregate GrossDividend for same Ticker and MIC and ExDate. This happens in hstech but not sure happens in ftse, but not harm
        df_records = df_records.groupby(["Ticker", "MIC", "ExDate", "DividendCcy", "IndexCode"], as_index=False).agg({"GrossDividend": "sum"})
        df_records["UpdatedOn"] = updated_on

        if not df_records.empty:
            logger.info(f"FtseDividendReader dividend records after merging\n%s", df_records.to_csv(sep="\t", index=False, lineterminator="\n").rstrip())

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
                    delete_sql = f"DELETE FROM {self.table_name_} WHERE Ticker = %s AND MIC = %s AND ExDate = %s AND IndexSource = %s"
                    cursor.execute(delete_sql, (ticker, mic, ex_date, index_source))

                    insert_sql = f"""
                        INSERT INTO {self.table_name_} ({', '.join(self.INSERT_COLUMNS)})
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """
                    cursor.execute(insert_sql, (ticker, mic, ex_date, dividend_ccy, gross_dividend, updated_on, index_code, index_source))

            self.pcfGenDbClnt_.commit()

        except Exception as e:
            self.pcfGenDbClnt_.rollback()
            err = f"FtseDividendReader:Failed to persist {dividend_file_fullpath} into {self.table_name_}. ex={e}"
            logger.exception(err)
            raise RuntimeError(err) from e

        logger.info(f"Persisted {dividend_file_fullpath} into {self.table_name_}.")
        return len(df_records)