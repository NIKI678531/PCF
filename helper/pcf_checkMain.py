from datetime import datetime
import os
import webbrowser

from pcf_checker import PcfChecker


pcfsrc = r"P:\ETFoperations\pcfgen_uat"

lstFunds = [
    ("HK-BRK-2L", "BRK_2LEV"),
    ("HK-TSLA-2L", "TSLA_2LEV"),
    ("HK-TSLA-2I", "TSLA_2INV"),
    ("HK-NVDA-2L", "NVDA_2LEV"),
    ("HK-NVDA-2I", "NVDA_2INV"),
    ("HK-COIN-2L", "COIN_2LEV"),
    ("HK-COIN-2I", "COIN_2INV"),
    ("HK-MSTR-2L", "MSTR_2LEV"),
    ("HK-MSTR-2I", "MSTR_2INV"),
    ("HK-GLD-L", "GOLD_LEV"),
    ("HK-BTCF-I", "BTCF_INV"),
    ("HK-BTCFUT", "BTCFUTURE"),
    ("HK-DCRFUT", "ETHFUTURE"),
]


def main():

    today_dt = datetime.now()

    for tradarcode, pcf_key in lstFunds:
        checker = PcfChecker(pcfsrc, pcf_key, tradarcode, today_dt)
        checker.check_pcf()
    

if __name__ == "__main__":
    raise SystemExit(main())