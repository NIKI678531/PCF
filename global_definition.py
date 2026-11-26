# define the global varables for whole project

g_lstFunds_am = [
    'HK-BRK-2L',
    'HK-TSLA-2L',
    'HK-TSLA-2I',
    'HK-NVDA-2L',
    'HK-NVDA-2I',
    'HK-COIN-2L',
    'HK-COIN-2I',
    'HK-MSTR-2L',
    'HK-MSTR-2I',
    'HK-GLD-L',
    'HK-BTCF-I',
    'HK-NDX-L',
    'HK-NDX-2I',
    'HK-BTCFUT', # although not LnI, but it has future underlying, treat as LnI
    'HK-DCRFUT', # although not LnI, but it has future underlying, treat as LnI
    'HK-UST20',
]

g_lstFunds_pm = [
    'HK-NIK-L',
    'HK-NIK-2I',
    'HK-HCE-L',
    'HK-HCE-2I',
    'HK-HSI-L',
    'HK-HSI-I',
    'HK-HSI-2I',
    'HK-CS3-L',
    'HK-SMSN-2I',
    'HK-SMSN-2L',
    'HK-HST-L',
    'HK-HST-2I',
    'HK-HCECC',
    'HK-CKCC',
    'HK-SKHY-2L',
    'CO-USTB',
    'CO-MMF',
    'CO-CUSF',
    'SG-WGBI',
    'SG-WGBI_X', # Only exists in program world, it is a listed share class of SG-WGBI
    'HK-CSAI',
    'HK-CSI5',
    'HK-CPTC',
    'CO-CHST',
    'HK-KIOX-125L',
    'HK-HSI', # only for case when there is dividend and override Markit pcf is needed.
    'HK-CKTH'
]