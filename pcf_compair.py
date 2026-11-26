# compare two files line by line and show the difference

import os
import sys
import logging
from logging.handlers import TimedRotatingFileHandler

lstFunds_am = [
    ('HK-BRK-2L', 'BRK_2LEV'),
    ('HK-TSLA-2L', 'TSLA_2LEV'),
    ('HK-TSLA-2I', 'TSLA_2INV'),
    ('HK-NVDA-2L', 'NVDA_2LEV'),
    ('HK-NVDA-2I', 'NVDA_2INV'),
    ('HK-COIN-2L', 'COIN_2LEV'),
    ('HK-COIN-2I', 'COIN_2INV'),
    ('HK-MSTR-2L', 'MSTR_2LEV'),
    ('HK-MSTR-2I', 'MSTR_2INV'),
    ('HK-GLD-L', 'GOLD_LEV'),
    ('HK-BTCF-I', 'BTCF_INV'),
    ('HK-NDX-L', 'NDX_LEV'),
    ('HK-NDX-2I', 'NDX_2INV'),
    ('HK-BTCFUT', 'BTCFUTURE'),
    ('HK-DCRFUT', 'ETHFUTURE'),
    ('HK-UST20', 'CSOPUST20'),
]

lstFunds_pm = [
    ('HK-NIK-L', 'NIK225_LEV'),
    ('HK-NIK-2I', 'NIK225_2INV'),
    ('HK-HCE-L', 'HSCEI_LEV'),
    ('HK-HCE-2I', 'HSCEI_2INV'),
    ('HK-HSI-L', 'HSI_LEV'),
    ('HK-HSI-I', 'HSI_INV'),
    ('HK-HSI-2I', 'HSI_2INV'),
    ('HK-CS3-L', 'CSI300_LEV'),
    ('HK-SMSN-2I', 'SMSN_2INV'),
    ('HK-SMSN-2L', 'SMSN_2LEV'),
    ('HK-HST-L', 'HSTECH_LEV'),
    ('HK-HST-2I', 'HSTECH_2INV'),
    ('HK-HCECC', 'HKHCECC'),
    ('HK-CKCC', 'HKCKCC'),
    ('HK-SKHY-2L', 'SKHY_2LEV'),
    # ('SG-WGBI', 'CSOPCGB'),
    # ('SG-WGBI_X', 'CSOPCGB'),
    ('HK-CSAI', 'HKCSAI'),
    ('HK-CSI5', 'CSOPCSI500'),
    ('HK-CPTC', 'HKCPTC'),
    # ('CO-CHST', 'COCHST'),
    ('HK-KIOX-125L', 'KIOX_125LEV'),
    # manual check for 'CO-USTB', 'CO-MMF', 'CO-CUSF', 'CO-CHST'
]

def compare_files_header(src, dest, type):
    # for ust20, there are 40 components. The order, ticker might be different for all lines.
    # to simplify the comparison, just compare the pcf header part. I think it should be good enough.

    if type in ['PCF', 'IOPVPCF']:
        header_end = ',Number of Fund Shares per Creation'
    elif type in ['HSBCPCF']:
        header_end = '6,'

    def read_pcf_header(file_path):
        header_lines = []
        with open(file_path, 'r') as file:
            for line in file:
                line = line.strip()
                header_lines.append(line)
                if line.startswith(header_end):
                    break
        return header_lines

    src_lines = read_pcf_header(src)
    dest_lines = read_pcf_header(dest)

    if len(src_lines) != len(dest_lines):
        logger.error(f"{src}")
        logger.error(f"{dest}")
        logger.error("different number of header lines")
        return False

    max_lines = len(src_lines)
    for i in range(max_lines):
        src_line = src_lines[i]
        dest_line = dest_lines[i]

        if src_line != dest_line:
            logger.error(f"{type} Header Line {i} is different:")
            logger.error(f"  PRD: {src_line}")
            logger.error(f"  UAT: {dest_line}")
    
def compare_files(src, dest, type):
    # open the file, can assume both files are text file.
    # read both file line by line, trip the space at begin and end. then compare both lines character by character.
    # if any difference, print the line number and the different content.
    with open(src, 'r') as f_src, open(dest, 'r') as f_dest:
        src_lines = f_src.readlines()
        dest_lines = f_dest.readlines()

        if len(src_lines) != len(dest_lines):
            logger.error(f"{src}")
            logger.error(f"{dest}")
            logger.error("different number of lines")
            return False

        max_lines = len(src_lines)
        for i in range(max_lines):
            src_line = src_lines[i].strip()
            dest_line = dest_lines[i].strip()

            if src_line != dest_line:
                logger.error(f"{type} Line {i} is different:")
                logger.error(f"  PRD: {src_line}")
                logger.error(f"  UAT: {dest_line}")

if __name__ == "__main__":
    
    logFileFullName = r'c:/temp/t13/PcfCompLog.txt'
    
    logger = logging.getLogger('Main')

    logger.setLevel(level=logging.DEBUG)

    # StreamHandler
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(level=logging.DEBUG)
    streamformatter = logging.Formatter('%(asctime)s-%(levelname)s: %(message)s')
    stream_handler.setFormatter(streamformatter)
    logger.addHandler(stream_handler)

    # FileHandler TimedRotating
    file_handler = TimedRotatingFileHandler(logFileFullName, encoding='utf-8', when='midnight', interval=1, backupCount=100)
    file_handler.setLevel(level=logging.INFO)
    formatter = logging.Formatter('%(asctime)s-%(levelname)s: %(message)s')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.info('Program Start ...')

    # src_path = r"P:\ETFoperations\{tradar_code}\Processed"
    src_path = r"P:\ETFoperations\pcfgen_uat\{tradar_code}"

    # dst_path = r"P:\ETFoperations\pcfgen_uat\{tradar_code}"
    dst_path = r"c:\temp\t8\{tradar_code}" # new generation

    file_date = "20261118"
    
    fund_list = lstFunds_pm
    # fund_list = lstFunds_am

    logger.info(f'Comparing files as of {file_date}...')

    for tradar_code, file_code in fund_list:
        logger.info(f">>> Comparing files for {tradar_code} ...")
        
        if tradar_code == "HK-GLD-L":
            tradar_code = "HK-GOLD-L"
            
        for pcf_type in ["PCF", "IOPVPCF", "HSBCPCF", "CSTN"]:
            if pcf_type == 'HSBCPCF':
                if tradar_code in ['HK-CS3-L']:
                    continue
                src_file = os.path.join(src_path.replace("{tradar_code}", tradar_code), f"PCF_{file_code}_{file_date}A.txt")
                dst_file = os.path.join(dst_path.replace("{tradar_code}", tradar_code), f"PCF_{file_code}_{file_date}A.txt")
            elif pcf_type == 'CSTN':
                if tradar_code not in ['HK-HST-L', 'HK-HST-2I']:
                    continue # only these two funds have CSTN pcf file, skip others
                src_file = os.path.join(src_path.replace("{tradar_code}", tradar_code), f"PCF_CSTN_{file_code}_{file_date}.csv")
                dst_file = os.path.join(dst_path.replace("{tradar_code}", tradar_code), f"PCF_CSTN_{file_code}_{file_date}.csv")
            else:
                src_file = os.path.join(src_path.replace("{tradar_code}", tradar_code), f"{pcf_type}_{file_code}_{file_date}.csv")
                dst_file = os.path.join(dst_path.replace("{tradar_code}", tradar_code), f"{pcf_type}_{file_code}_{file_date}.csv")

            if tradar_code == "HK-UST20":
                logger.info(f"Comparing only header for {tradar_code}")
                compare_files_header(src_file, dst_file, pcf_type)
            else:
                compare_files(src_file, dst_file, pcf_type)

            
        
        logger.info(f"<<< Finished comparing files for {tradar_code}.\n")