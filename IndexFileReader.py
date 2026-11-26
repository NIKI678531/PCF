import csv
import logging
from decimal import Decimal, InvalidOperation


logger = logging.getLogger('Main')


class IndexFileReader:
    # map index file columns to t_indx_weight columns
    COLUMN_MAPPING = {
        "Opndate": "EffectiveDate",
        "Exch": "MIC",
        "Lcal Cde": "Ticker",
        "Lcal Ccy": "Currency",
        "Cls Price": "Price",
        "FX": "Fx",
        "Idx Shs": "SharesInIndex",
        "Pct Idx Wgt": "Weight",
        "Idx Cde": "IndexCode",
    }

    # columns of t_indx_weight
    INSERT_COLUMNS = (
        "EffectiveDate",
        "IndexSource",
        "IndexCode",
        "MIC",
        "Ticker",
        "Currency",
        "Price",
        "Fx",
        "Weight",
        "FileDate",
        "SharesInIndex",
    )

    NUMERIC_COLUMNS = {
        "Price",
        "Fx",
        "Weight",
        "SharesInIndex",
    }

    def __init__(self, pcfGenDbClnt, table_name):
        self.pcfGenDbClnt_ = pcfGenDbClnt
        self.table_name_ = table_name

    def _BuildRecord(self, csv_row, row_number, file_date_ymd, index_source):
        record = {
            "IndexSource": index_source,
            "FileDate": file_date_ymd,
        }

        for csv_column, tbl_column in self.COLUMN_MAPPING.items():
            raw_value = csv_row.get(csv_column)

            if tbl_column in self.NUMERIC_COLUMNS:
                record[tbl_column] = self._ParseDecimal(raw_value, csv_column, row_number)
            else:
                record[tbl_column] = "" if raw_value is None else raw_value.strip()

        return record

    def ReadFile(self, index_file_path, index_source, file_date_ymd):
        records = []
        with open(index_file_path, newline="", encoding="utf-8-sig") as index_file:
            reader = csv.DictReader(index_file)

            # start row skip header, assuming header is at row 1
            for row_number, csv_row in enumerate(reader, start=2):
                if self._IsBlankRow(csv_row):
                    continue

                records.append(self._BuildRecord(csv_row, row_number, file_date_ymd, index_source))

        try:
            with self.pcfGenDbClnt_.cursor() as cursor:
                effective_date = records[0]["EffectiveDate"]
                cursor.execute(
                    f"DELETE FROM {self.table_name_} WHERE EffectiveDate = %s AND IndexSource = %s",
                    (effective_date, index_source),
                )

                placeholders = ", ".join(["%s"] * len(self.INSERT_COLUMNS))
                insert_sql = f"""
                    INSERT INTO {self.table_name_} ({', '.join(self.INSERT_COLUMNS)})
                    VALUES ({placeholders})
                """

                for record in records:
                    cursor.execute(insert_sql, tuple(record[column] for column in self.INSERT_COLUMNS))

            self.pcfGenDbClnt_.commit()

        except Exception as e:
            self.pcfGenDbClnt_.rollback()
            err = f"IndexFileReader:Failed to persist {index_file_path} into {self.table_name_}. ex={e}"
            logger.exception(err)
            raise RuntimeError(err) from e

        logger.info(f"Persisted {index_file_path} into {self.table_name_}.")
        return len(records)

    def _ParseDecimal(self, value, column_name, row_number):
        try:
            decimal_value = Decimal(value)
            if column_name == "Pct Idx Wgt":
                decimal_value = decimal_value / 100
        except InvalidOperation as exc:
            raise ValueError(f"Invalid numeric at {column_name} rows {row_number}: {value}") 

        return decimal_value

    def _IsBlankRow(self, csv_row):
        opn_date = csv_row.get("Opndate")
        local_code = csv_row.get("Lcal Cde")
        return (opn_date is None or str(opn_date).strip() == "") and (local_code is None or str(local_code).strip() == "")