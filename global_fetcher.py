"""
美股 / 港股資料擷取器（yfinance）
===================================
用 yfinance 抓美股、港股歷史日K，轉成 sop_screener.py 需要的 OHLCV 格式，
直接串進 screen_stocks()。跟 twse_fetcher.py（台股/TWSE）是同一層級的另外
兩個資料來源模組，用法對稱，可以互相搭配用。

【代號格式】
- 美股：直接用代號本身，如 "AAPL", "TSLA", "NVDA"（大小寫皆可，內部會轉大寫）
- 港股：可以輸入 "700" 或 "0700"，會自動補成 yfinance 需要的 "0700.HK" 格式

【使用方式】
    from global_fetcher import fetch_and_screen_us, fetch_and_screen_hk

    us_result = fetch_and_screen_us(["AAPL", "TSLA", "NVDA"])
    hk_result = fetch_and_screen_hk(["0700", "9988", "3690"])  # 騰訊/阿里/美團

【注意事項】
- yfinance 資料來源是 Yahoo Finance，非官方 API，穩定性與資料完整度不像
  TWSE 官方 API 那麼有保證，遇到抓取失敗可重試。
- Yahoo Finance 對「雲端/機房 IP」（例如 Streamlit Community Cloud）常會擋查
  股票名稱用的 API（歷史K線的端點通常還抓得到，名稱查詢的端點比較容易被擋），
  導致部署到雲端後名稱查得到但顯示空白。這裡透過 curl_cffi 模擬瀏覽器連線
  （impersonate="chrome"）繞過這個限制，本機與雲端都用同一個 session。
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf
from curl_cffi import requests as cffi_requests

from sop_screener import DEFAULT_PARAMS, SOPParams, screen_stocks

# 模擬瀏覽器的連線 session，繞過 Yahoo Finance 對雲端/機房 IP 的封鎖。
# 整個模組共用同一個 session，避免每次呼叫都重新建立連線。
_SESSION = cffi_requests.Session(impersonate="chrome")

# 股票代號 -> 股票名稱 對照表快取，同一次程式執行只查一次
_name_cache: dict[str, str] = {}


def normalize_us_ticker(code: str) -> str:
    """美股代號正規化：去除頭尾空白、轉大寫。"""
    return code.strip().upper()


def normalize_hk_ticker(code: str) -> str:
    """港股代號正規化：'700' 或 '0700' -> '0700.HK'（yfinance 需要4位數字+.HK）。"""
    code = code.strip().upper().replace(".HK", "")
    return f"{code.zfill(4)}.HK"


def fetch_yf_ohlcv(ticker: str, months_back: int = 7) -> pd.DataFrame:
    """
    用 yfinance 抓某檔股票最近 months_back 個月的日K，轉成標準 OHLCV DataFrame
    （欄位: Date/Open/High/Low/Close/Volume）。查無資料會丟例外。
    """
    period_days = months_back * 31
    t = yf.Ticker(ticker, session=_SESSION)
    df = t.history(period=f"{period_days}d", interval="1d", auto_adjust=False)
    if df.empty:
        raise ValueError(f"{ticker} 抓不到任何資料，請確認代號是否正確")

    df = df.reset_index()
    df = df.rename(columns={df.columns[0]: "Date"})  # 依市場不同可能叫 Date 或 Datetime
    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
    df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna().sort_values("Date").reset_index(drop=True)
    return df


def get_yf_name(ticker: str) -> str:
    """查詢股票名稱（yfinance）；查不到就回傳空字串，不會拋例外。"""
    if ticker in _name_cache:
        return _name_cache[ticker]
    try:
        info = yf.Ticker(ticker, session=_SESSION).info
        name = info.get("longName") or info.get("shortName") or ""
    except Exception:
        name = ""
    _name_cache[ticker] = name
    return name


def _fetch_and_screen(
    tickers: list[str],
    normalize_fn,
    months_back: int,
    params: SOPParams,
) -> pd.DataFrame:
    data = {}
    for raw in tickers:
        ticker = normalize_fn(raw)
        try:
            data[ticker] = fetch_yf_ohlcv(ticker, months_back=months_back)
        except Exception as e:
            print(f"[警告] {ticker} 抓取失敗，已跳過：{e}")

    if not data:
        raise ValueError("所有股票代號都抓取失敗，沒有資料可供篩選")

    result = screen_stocks(data, params=params)
    result.insert(1, "name", result["symbol"].map(get_yf_name).fillna(""))
    return result


def fetch_and_screen_us(
    tickers: list[str],
    months_back: int = 7,
    params: SOPParams = DEFAULT_PARAMS,
) -> pd.DataFrame:
    """美股篩選：直接給美股代號，如 ["AAPL", "TSLA", "NVDA"]。"""
    return _fetch_and_screen(tickers, normalize_us_ticker, months_back, params)


def fetch_and_screen_hk(
    tickers: list[str],
    months_back: int = 7,
    params: SOPParams = DEFAULT_PARAMS,
) -> pd.DataFrame:
    """港股篩選：代號可輸入 "700" 或 "0700"，會自動補成 yfinance 格式。"""
    return _fetch_and_screen(tickers, normalize_hk_ticker, months_back, params)


if __name__ == "__main__":
    print("=== 美股範例 ===")
    print(fetch_and_screen_us(["AAPL", "TSLA"]).to_string(index=False))
    print("\n=== 港股範例 ===")
    print(fetch_and_screen_hk(["0700", "9988"]).to_string(index=False))
