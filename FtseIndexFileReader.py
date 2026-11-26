import csv
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime

from IndexFileReader import IndexFileReader

logger = logging.getLogger('Main')

class FtseIndexFileReader(IndexFileReader):
    # map index file columns to t_indx_weight columns
    COLUMN_MAPPING = {
        "_Date": "EffectiveDate", # Self defined column, not existing in index file
        "MIC": "MIC",
        "BBG Ticker": "Ticker",
        "ISO Currency Code": "Currency",
        "Price": "Price",
        "_FX": "Fx", # Self defined column, not existing in index file, e.g. 1
        "Index Shares": "SharesInIndex",
        "% Weight in Index": "Weight",
        "_IdxCde": "IndexCode", # Self defined column, not existing in index file, e.g. KRTECH
    }

    # columns of t_indx_weight
    # inherited from IndexFileReader

    # Map index file columns to security table columns
    SECURITY_TBL_MAPPING = {
        "BBG Ticker": "Ticker",
        "Constituent Name": "Name",
        "ISIN": "Isin",
        "MIC": "MIC"
    }

    SECURITY_INSERT_COLUMNS = (
        "Ticker",
        "Isin",
        "Name",
        "MIC",
        "UpdatedOn"
    )

    def __init__(self, pcfGenDbClnt, table_name):
        super().__init__(pcfGenDbClnt, table_name)

    def _IsBlankRow(self, csv_row):
        Cons_Code = csv_row.get("Cons Code")
    
        return Cons_Code is None or str(Cons_Code).strip() == ""
    
    def _BuildRecord(self, csv_row, row_number, file_date_ymd, index_source):
        record = {}

        # here record should be in tbl_column
        record["IndexSource"] = index_source
        record["FileDate"] = file_date_ymd

        record["EffectiveDate"] = file_date_ymd
        record["Fx"] = 1
        record["IndexCode"] = "KRTECH"

        for csv_column, tbl_column in self.COLUMN_MAPPING.items():
            if csv_column.startswith("_"):
                continue
            try:
                raw_value = csv_row.get(csv_column)

                if tbl_column in self.NUMERIC_COLUMNS:
                    if tbl_column == 'Weight':
                        raw_value = raw_value.strip('%') if raw_value else raw_value
                    record[tbl_column] = self._ParseDecimal(raw_value, csv_column, row_number)
                elif tbl_column == 'Ticker':
                    record[tbl_column] = raw_value.split(" ")[0] # e.g. 259960 KP -> 259960
                else:
                    record[tbl_column] = "" if raw_value is None else raw_value.strip()
            except Exception as e:
                logger.error(f"Error processing row {csv_row}, column {csv_column}: {e}")
                raise e
        return record
    
    def ReadFile(self, index_file_path, index_source, file_date_ymd):
        index_records = []
        security_records = []

        with open(index_file_path, newline="", encoding="utf-8-sig") as index_file:
            # skip the first two lines
            for _ in range(3):
                next(index_file)
            reader = csv.DictReader(index_file)

            updatedon = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for csv_row in reader:
                row_number = reader.line_num + 3
                
                if csv_row["Cons Code"].startswith("XXXX"):
                    break

                if self._IsBlankRow(csv_row):
                    continue

                index_records.append(self._BuildRecord(csv_row, row_number, file_date_ymd, index_source))

                security_rec = {}
                security_rec["UpdatedOn"] = updatedon
                for csv_column, tbl_column in self.SECURITY_TBL_MAPPING.items():
                    raw_value = csv_row.get(csv_column)
                    if csv_column == "BBG Ticker":
                        ticker = raw_value.split(" ")[0] # 042700 KP -> 042700
                        security_rec[tbl_column] = ticker
                    else:
                        security_rec[tbl_column] = "" if raw_value is None else raw_value.strip()

                security_records.append(security_rec)
        
        # update the t_indx_weight          
        try:
            with self.pcfGenDbClnt_.cursor() as cursor:
                effective_date = index_records[0]["EffectiveDate"] # this should be same for all index records in the file

                cursor.execute(
                    f"DELETE FROM {self.table_name_} WHERE EffectiveDate = %s AND IndexSource = %s",
                    (effective_date, index_source),
                )

                placeholders = ", ".join(["%s"] * len(self.INSERT_COLUMNS))
                insert_sql = f"INSERT INTO {self.table_name_} ({', '.join(self.INSERT_COLUMNS)}) VALUES ({placeholders})"

                for record in index_records:
                    cursor.execute(insert_sql, tuple(record[column] for column in self.INSERT_COLUMNS))

            self.pcfGenDbClnt_.commit()

        except Exception as e:
            self.pcfGenDbClnt_.rollback()
            err = f"FtseIndexFileReader:Failed to persist {index_file_path} into {self.table_name_}. ex={e}"
            logger.exception(err)
            raise RuntimeError(err) from e
        logger.info(f"Persisted {index_file_path} into {self.table_name_}.")

        # update the t_security_code. Because FTSE index file provide the security name and isin, we can update the security table accordingly.
        security_table_name = "t_security_code"
        try:
            with self.pcfGenDbClnt_.cursor() as cursor:

                # for those columns of SECURITY_INSERT_COLUMNS not in SECURITY_INSERT_COLUMNS will be default value e.g. NULL
                placeholders = ", ".join(["%s"] * len(self.SECURITY_INSERT_COLUMNS))
                insert_sql = f"""
                    INSERT INTO {security_table_name} ({', '.join(self.SECURITY_INSERT_COLUMNS)})
                    VALUES ({placeholders})
                """

                for record in security_records:
                    ticker = record["Ticker"]
                    mic = record["MIC"]
                    cursor.execute( f"DELETE FROM {security_table_name} WHERE Ticker = %s AND MIC = %s", (ticker, mic))
                                    
                    cursor.execute(insert_sql, tuple(record[column] for column in self.SECURITY_INSERT_COLUMNS))

            self.pcfGenDbClnt_.commit()

        except Exception as e:
            self.pcfGenDbClnt_.rollback()
            err = f"FtseIndexFileReader:Failed to persist {index_file_path} into {security_table_name}. ex={e}"
            logger.exception(err)
            raise RuntimeError(err) from e
        logger.info(f"Persisted {index_file_path} into {security_table_name}.")

        return len(index_records)