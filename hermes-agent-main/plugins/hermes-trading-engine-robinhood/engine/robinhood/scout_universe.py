"""Scout universe: the fixed, rule-based list of liquid US symbols the scout
sweeps, plus the inverse-ETF map and the leveraged blacklist.

Kept separate from the ranking engine so the *what we scan* (a data list)
and the *how we rank* (scout.py) evolve independently. This is deliberately
a curated LIQUID universe, not "every ticker" — a $10k account cannot
trade illiquid micro-caps, and the liquidity screen would reject them
anyway. Scanning the literal whole market server-side is a separate path
(Robinhood's run_scan), wired only once its schema is verified.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List

# 1x inverse ETFs → their underlying: the cash-account-safe downside tools
# (bought LONG). No leverage.
INVERSE_ETFS: Dict[str, str] = {
    "SH": "SPY", "PSQ": "QQQ", "DOG": "DIA", "RWM": "IWM",
    "EUM": "EEM", "MYY": "MDY", "TBF": "TLT", "BITI": "IBIT",
}

# Leveraged / volatility products: never scanned, never charted (daily
# rebalancing decay ruins multi-day holds regardless of direction).
LEVERAGED_BLACKLIST: FrozenSet[str] = frozenset({
    "SQQQ", "TQQQ", "SDS", "SSO", "SPXU", "UPRO", "SPXL", "SPXS",
    "SDOW", "UDOW", "TZA", "TNA", "URTY", "SRTY", "SOXL", "SOXS",
    "FAZ", "FAS", "LABU", "LABD", "NUGT", "DUST", "JNUG", "JDST",
    "YINN", "YANG", "TMF", "TMV", "BOIL", "KOLD", "UCO", "SCO",
    "UVXY", "SVXY", "VXX", "VIXY", "UVIX", "SVIX", "BITX", "ETHU",
    "TSLL", "TSLQ", "NVDL", "NVDS", "AAPU", "AAPD", "MSFU", "CONL",
    "USD", "DIG", "ERX", "ERY", "GUSH", "DRIP", "WEBL", "WEBS",
})

# Curated liquid universe by tier. Edit deliberately; never mid-analysis.
_ETFS: List[str] = [
    # broad / size / style / factor
    "SPY", "QQQ", "DIA", "IWM", "MDY", "IJH", "IJR", "RSP", "VTI", "VOO",
    "VUG", "VTV", "VB", "VO", "VYM", "SCHD", "DVY", "QUAL", "MTUM", "USMV",
    "SPLV", "SPHB", "MGK", "IWF", "IWD", "IVW", "IVE", "ARKK", "ARKG", "ARKW",
    # sectors + industries
    "XLE", "XLF", "XLV", "XLI", "XLP", "XLU", "XLB", "XLY", "XLK", "XLRE",
    "XLC", "SMH", "SOXX", "XBI", "IBB", "KRE", "KBE", "XHB", "ITB", "XOP",
    "OIH", "GDX", "GDXJ", "XME", "IYT", "JETS", "XRT", "ITA", "PPA", "TAN",
    "ICLN", "FAN", "HACK", "CIBR", "IGV", "SKYY", "ROBO", "BOTZ", "FINX",
    "VNQ", "IYR", "REZ", "KIE", "PBW", "LIT", "URA", "REMX", "COPX", "SIL",
    "MOO", "WOOD", "PHO", "XSD", "PBJ", "IHI", "XPH", "IHF", "XTL", "XAR",
    # geography
    "EEM", "EFA", "VEA", "VWO", "IEFA", "IEMG", "FXI", "MCHI", "KWEB", "ASHR",
    "EWZ", "EWJ", "EWG", "EWU", "EWY", "EWT", "INDA", "EWW", "EWC", "EWA",
    "EZA", "TUR", "EWP", "EWQ", "EWL", "ARGT", "ILF", "EWH", "EWS", "THD",
    "VGK", "EPI", "EIDO", "EPHE", "EWM", "NORW", "PIN", "GXC",
    # bonds / rates / credit
    "TLT", "IEF", "SHY", "IEI", "AGG", "BND", "LQD", "HYG", "JNK", "TIP",
    "EMB", "MBB", "BIL", "SHV", "VCIT", "VCSH", "MUB", "BKLN", "SJNK", "VTIP",
    # commodities / metals / crypto proxies
    "GLD", "IAU", "SLV", "GDX", "USO", "BNO", "UNG", "DBC", "DBA", "CPER",
    "PPLT", "PALL", "CORN", "WEAT", "SOYB", "URNM", "IBIT", "ETHA", "FBTC",
    # 1x inverse
    "SH", "PSQ", "DOG", "RWM", "EUM", "MYY", "TBF", "BITI",
]

_STOCKS: List[str] = [
    # mega-cap tech / comms
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO",
    "ORCL", "CRM", "ADBE", "AMD", "QCOM", "TXN", "INTC", "MU", "AMAT", "LRCX",
    "KLAC", "ADI", "NXPI", "MRVL", "SNPS", "CDNS", "FTNT", "PANW", "CRWD",
    "PLTR", "SNOW", "NOW", "INTU", "WDAY", "TEAM", "DDOG", "NET", "ZS", "MDB",
    "ANET", "SMCI", "DELL", "HPQ", "HPE", "CSCO", "IBM", "ACN", "UBER", "ABNB",
    "SHOP", "SQ", "PYPL", "COIN", "HOOD", "SNAP", "PINS", "RBLX", "SPOT",
    "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS", "WBD", "PARA", "EA", "TTWO",
    "ROKU", "ZM", "DOCU", "OKTA", "TWLO", "U", "PATH", "AI", "SOUN",
    # financials
    "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "BLK", "AXP", "V", "MA",
    "COF", "USB", "PNC", "TFC", "BK", "STT", "MET", "PRU", "AIG", "ALL",
    "TRV", "PGR", "CB", "AFL", "MMC", "AON", "ICE", "CME", "SPGI", "MCO",
    "FIS", "FISV", "GPN", "SOFI", "ALLY", "DFS", "SYF", "NDAQ", "KKR", "APO",
    "BX", "CG", "ARES", "OWL",
    # healthcare
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "BMY",
    "AMGN", "GILD", "VRTX", "REGN", "ISRG", "MDT", "SYK", "BSX", "ELV", "CI",
    "CVS", "HUM", "CNC", "ZTS", "BDX", "EW", "DXCM", "IDXX", "IQV", "MRNA",
    "BIIB", "ILMN", "GEHC", "RMD", "A", "MTD", "WST", "HCA", "MCK", "COR",
    "CAH", "HOLX", "ALGN", "PODD", "VEEV", "DVA",
    # consumer
    "WMT", "COST", "PG", "KO", "PEP", "MCD", "SBUX", "NKE", "LULU", "TGT",
    "HD", "LOW", "TJX", "ROST", "DG", "DLTR", "KHC", "MDLZ", "GIS", "K",
    "HSY", "CL", "KMB", "EL", "CLX", "CHD", "STZ", "MNST", "KDP", "SYY",
    "KR", "ADM", "MO", "PM", "YUM", "CMG", "DPZ", "DRI", "MAR", "HLT", "BKNG",
    "EXPE", "RCL", "CCL", "NCLH", "MGM", "LVS", "WYNN", "CZR", "DKNG", "F",
    "GM", "RIVN", "LCID", "APTV", "BWA", "LEA", "GPC", "AZO", "ORLY", "ULTA",
    "BBY", "DECK", "RL", "TPR", "VFC", "YETI", "CROX", "W", "CHWY", "ETSY",
    "EBAY", "DASH", "GRAB",
    # energy / industrials / materials
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "MPC", "PSX", "VLO", "HAL",
    "DVN", "FANG", "HES", "WMB", "KMI", "OKE", "LNG", "BKR", "MRO", "APA",
    "CTRA", "TRGP", "CAT", "DE", "BA", "GE", "HON", "MMM", "EMR", "ETN",
    "PH", "ITW", "ROK", "CMI", "PCAR", "UNP", "CSX", "NSC", "UPS", "FDX",
    "LMT", "RTX", "NOC", "GD", "LHX", "HII", "TDG", "CARR", "OTIS", "JCI",
    "AME", "ROP", "FTV", "DOV", "IR", "XYL", "PNR", "WAB", "URI", "PWR",
    "FAST", "GWW", "PAYX", "ADP", "VRSK", "CTAS", "RSG", "WM", "GNRC",
    "LIN", "APD", "SHW", "ECL", "FCX", "NEM", "NUE", "STLD", "DOW", "DD",
    "PPG", "ALB", "CF", "MOS", "FMC", "VMC", "MLM", "IP", "PKG", "CE",
    "CTVA", "LYB", "EMN", "RPM", "AVTR",
    # utilities / real estate
    "NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "XEL", "ED", "PEG", "PCG",
    "WEC", "ES", "EIX", "AWK", "DTE", "PPL", "FE", "AEE", "CMS", "CNP",
    "AMT", "PLD", "CCI", "EQIX", "PSA", "O", "SPG", "WELL", "DLR", "VICI",
    "SBAC", "AVB", "EQR", "EXR", "MAA", "INVH", "ARE", "VTR", "IRM", "WY",
    # high-volume growth / meme / recent IPO
    "MSTR", "MARA", "RIOT", "CLSK", "SMR", "OKLO", "IONQ", "RGTI", "QBTS",
    "AFRM", "UPST", "CVNA", "RDDT", "APP", "ARM", "DELL", "GTLB",
    "S", "ESTC", "FSLR", "ENPH", "SEDG", "RUN", "PLUG", "BE", "CHPT", "QS",
    "BLNK", "NIO", "XPEV", "LI", "BABA", "JD", "PDD", "BIDU", "TCOM", "NTES",
]

# De-duped, blacklist-purged, deterministic order.
SCOUT_UNIVERSE: List[str] = sorted(
    {s.upper() for s in (_ETFS + _STOCKS)} - LEVERAGED_BLACKLIST
)
