from datetime import datetime
import csv
from decimal import Decimal, InvalidOperation
import os
import logging

class PcfChecker:
    def __init__(self, pcfsrc: str, pcf_keyword, tradarcode: str, today_dt: datetime):
        self.pcfsrc_ = pcfsrc
        self.pcfkeyword_ = pcf_keyword
        self.tradarcode_ = tradarcode
        self.today_dt_ = today_dt

        self.logger_ = logging.getLogger('Main')
        self.report_items_ = []
        self.header_value_deviation_ = Decimal("0.0001")

    def _get_pcf_pair(self):
        if self.tradarcode_ == "HK-GLD-L":
            pcf_path = os.path.join(self.pcfsrc_, "HK-GOLD-L")
        else:
            pcf_path = os.path.join(self.pcfsrc_, self.tradarcode_)

        today_filename = os.path.join(pcf_path, f"PCF_{self.pcfkeyword_}_{self.today_dt_.strftime('%Y%m%d')}.csv")

        try:
            filename_prefix = f"PCF_{self.pcfkeyword_}_"
            filename_suffix = ".csv"
            today_fullname = os.path.normcase(os.path.abspath(today_filename))
            pcf_files = []

            for entry in os.scandir(pcf_path):
                if not entry.is_file():
                    continue
                if not entry.name.startswith(filename_prefix) or not entry.name.endswith(filename_suffix):
                    continue

                file_date = entry.name[len(filename_prefix):-len(filename_suffix)]
                if len(file_date) != 8 or not file_date.isdigit():
                    continue

                # skip the today file to find the most recent previous file
                if os.path.normcase(os.path.abspath(entry.path)) == today_fullname:
                    continue

                pcf_files.append(entry.path)

            pcf_files.sort(key=os.path.getmtime, reverse=True)

        except FileNotFoundError:
            return None

        if not pcf_files:
            return None

        yesterday_filename = pcf_files[0]

        return today_filename, yesterday_filename

    def _read_pcf_values(self, filename):
        header_values = {}
        component_values = {}
        component_columns = None
        company_name_index = None

        with open(filename, newline="", encoding="utf-8-sig") as pcf_file:
            reader = csv.reader(pcf_file)
            for row_number, row in enumerate(reader, start=1):
                cells = [cell.strip() for cell in row] # cells in the current row

                if not any(cells):
                    continue

                if component_columns is None and "Company Name" in cells:
                    component_columns = cells
                    company_name_index = cells.index("Company Name")
                    continue

                if component_columns is None:
                    if cells[0]:
                        key = cells[0]
                        value = cells[1] if len(cells) > 1 else ""
                    elif len(cells) > 1 and cells[1]:
                        key = cells[1]
                        value = cells[2] if len(cells) > 2 else ""
                    else:
                        continue

                    header_values[key] = value
                    continue

                if len(cells) <= company_name_index or not cells[company_name_index]:
                    continue

                company_name = cells[company_name_index]

                component_values[company_name] = {}
                for column_index, column_name in enumerate(component_columns):
                    if not column_name or column_index == company_name_index:
                        continue

                    component_values[company_name][column_name] = cells[column_index] if column_index < len(cells) else ""

        if component_columns is None:
            raise ValueError(f"Component header not found in {filename}")

        return header_values, component_values

    def _print_pcf_values(self, title, header_values, component_values):
        print(title)
        print("Header:")
        for key, value in header_values.items():
            print(f"  {key}: {value}")

        print("Components:")
        for company_name, component_values_by_column in component_values.items():
            print(f"  {company_name}:")
            for column_name, value in component_values_by_column.items():
                print(f"    {column_name}: {value}")

    def _save_report_item(self, section, issue, key=None, today_line=None, yesterday_line=None):
        self.report_items_.append({
            "section": section,
            "issue": issue,
            "key": key,
            "today_line": today_line,
            "yesterday_line": yesterday_line,
        })

    def _print_report_items(self):
        for section in ["header", "component"]:
            section_items = [item for item in self.report_items_ if item["section"] == section]
            if not section_items:
                continue

            self.logger_.error(f"[{section}]")
            for item in section_items:
                issue = item["issue"]
                key = item.get("key")
                today_line = item.get("today_line")
                yesterday_line = item.get("yesterday_line")
                key_text = f" | Key: {key}" if key else ""
                self.logger_.error(f"{issue}{key_text}")
                self.logger_.error(f"Tod: {today_line}")
                self.logger_.error(f"Yes: {yesterday_line}")

    def _format_header_line(self, key, value):
        return [key, value]

    def _format_component_line(self, company_name, component_values):
        line = [company_name]
        for value in component_values.values():
            line.append(value)
        return line

    def _format_component_value_line(self, company_name, column_name, value):
        return [company_name, column_name, value]

    def _to_decimal(self, value):
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError):
            return None

    def _values_match(self, key, today_value, yesterday_value):
        today_number = self._to_decimal(today_value)
        yesterday_number = self._to_decimal(yesterday_value)

        if 'Estimated Cash' in key:
            threshold = 0.2 # 20% change per day expected
        elif 'NAV per Share' in key:
            threshold = 0.1 # 10% change per day expected
        elif 'Price' in key:
            threshold = 0.15 # 15 % change price per day expected
        else:
            threshold = 0.01

        if today_number is not None and yesterday_number is not None:
            return abs(today_number - yesterday_number) / yesterday_number <= threshold
        else:
            if 'Trade Date' in key:
                today_trade_date = datetime.strptime(today_value, "%d/%m/%Y")
                yesterday_trade_date = datetime.strptime(yesterday_value, "%d/%m/%Y")
                diff_days = (today_trade_date - yesterday_trade_date).days
                return diff_days <= 4 # assume today and yesterday are within 4 days for Trade Date counting the holiday
            else:    
                return today_value == yesterday_value

    def _compare_header_values(self, today_headers, yesterday_headers):
        is_matched = True

        if len(today_headers) != len(yesterday_headers):
            self._save_report_item(
                "header",
                "key_count_mismatch",
                today_line=[f"count={len(today_headers)}"],
                yesterday_line=[f"count={len(yesterday_headers)}"],
            )
            is_matched = False

        for key in sorted(set(today_headers) | set(yesterday_headers)):
            if key not in today_headers:
                self._save_report_item(
                    "header",
                    "missing_in_today",
                    today_line=None,
                    yesterday_line=self._format_header_line(key, yesterday_headers[key]),
                )
                is_matched = False
                continue
            if key not in yesterday_headers:
                self._save_report_item(
                    "header",
                    "missing_in_yesterday",
                    today_line=self._format_header_line(key, today_headers[key]),
                    yesterday_line=None,
                )
                is_matched = False
                continue

            if not self._values_match(key, today_headers[key], yesterday_headers[key]):
                self._save_report_item(
                    "header",
                    "value_mismatch",
                    today_line=self._format_header_line(key, today_headers[key]),
                    yesterday_line=self._format_header_line(key, yesterday_headers[key]),
                )
                is_matched = False

        return is_matched

    def _compare_component_values(self, today_components, yesterday_components):
        is_matched = True

        if len(today_components) != len(yesterday_components):
            self._save_report_item(
                "component",
                "holding_count_mismatch",
                today_line=[f"count={len(today_components)}"],
                yesterday_line=[f"count={len(yesterday_components)}"],
            )
            is_matched = False

        for company_name in sorted(set(today_components) | set(yesterday_components)):
            if company_name not in today_components:
                self._save_report_item(
                    "component",
                    "missing_in_today",
                    company_name,
                    today_line=None,
                    yesterday_line=self._format_component_line(company_name, yesterday_components[company_name]),
                )
                is_matched = False
                continue
            if company_name not in yesterday_components:
                self._save_report_item(
                    "component",
                    "missing_in_yesterday",
                    company_name,
                    today_line=self._format_component_line(company_name, today_components[company_name]),
                    yesterday_line=None,
                )
                is_matched = False
                continue

            today_component = today_components[company_name]
            yesterday_component = yesterday_components[company_name]

            for column_name in today_component:
                report_key = f"{company_name} - {column_name}"
                # if today_component[column_name] != yesterday_component[column_name]:
                if not self._values_match(column_name, today_component[column_name], yesterday_component[column_name]):
                    self._save_report_item(
                        "component",
                        "value_mismatch",
                        report_key,
                        today_line=self._format_component_value_line(company_name, column_name, today_component[column_name]),
                        yesterday_line=self._format_component_value_line(company_name, column_name, yesterday_component[column_name]),
                    )
                    is_matched = False

        return is_matched
    
    def check_pcf(self):
        self.report_items_ = []

        pcf_pair = self._get_pcf_pair()
        if pcf_pair is None:
            self.logger_.error(f"PCF pair not found for {self.tradarcode_} ({self.pcfkeyword_})")
            return False

        today_file, yesterday_file = pcf_pair
        self.logger_.info(f"Checking PCF for {self.tradarcode_} ({self.pcfkeyword_})")

        try:
            today_headers, today_components = self._read_pcf_values(today_file)
            yesterday_headers, yesterday_components = self._read_pcf_values(yesterday_file)
        except FileNotFoundError as ex:
            self.logger_.error(f"PCF file not found: {ex.filename}")
            return False
        except ValueError as ex:
            self.logger_.error(str(ex))
            return False

        headers_matched = self._compare_header_values(today_headers, yesterday_headers)
        components_matched = self._compare_component_values(today_components, yesterday_components)
        is_matched = headers_matched and components_matched
        if is_matched:
            self.logger_.info(f"PCF check passed for {self.tradarcode_}")
        else:
            self.logger_.error(f"PCF check failed for {self.tradarcode_}")
            self._print_report_items()

        return is_matched

        