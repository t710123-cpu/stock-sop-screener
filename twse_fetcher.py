"""
TWSE（台灣證交所）個股日成交資訊擷取器
======================================
只需要股票代號，自動呼叫 TWSE 公開資訊觀測站的 STOCK_DAY API 抓歷史日K，
轉成 sop_screener.py 需要的 OHLCV DataFrame 格式，直接串進 screen_stocks()。

【重要限制】
- 只支援「上市」股票（TWSE STOCK_DAY API）。「上櫃」股票（TPEx）需要另一組
  API（tpex.org.tw），目前尚未實作，之後有需要可以再加一個 fetch_tpex_ohlcv()。
- TWSE STOCK_DAY 一次只回傳「單一月份」的資料，所以要抓到 60MA 需要的 120+
  個交易日，程式會自動迴圈往前抓好幾個月再合併，預設抓 7 個月。
- 每次呼叫 API 之間會 sleep 一下（預設0.3秒），避免短時間內打太多次被 TWSE 擋。
- 沒有做本地快取，每次執行都會重新對 TWSE 發請求；如果要頻繁重跑同一批股票，
  建議自行把抓回來的 DataFrame 存成 CSV 快取。

【使用方式】
    from twse_fetcher import fetch_and_screen

    result = fetch_and_screen(["2330", "2317", "2603"])
    print(result.to_string(index=False))

    # 或想拿到原始 OHLCV 資料自己處理：
    from twse_fetcher import fetch_twse_ohlcv
    df_2330 = fetch_twse_ohlcv("2330")
"""

from __future__ import annotations

import time
from datetime import date

import pandas as pd
import requests

from sop_screener import DEFAULT_PARAMS, SOPParams, screen_stocks

TWSE_STOCK_DAY_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TWSE_STOCK_DAY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"

# 股票代號 -> 股票名稱 對照表快取，同一次程式執行只打一次 API
_stock_name_cache: dict[str, str] = {}


def _roc_date_to_iso(roc_str: str) -> str:
    """把 TWSE 回傳的民國年日期字串（如 '113/08/01'）轉成 ISO 格式（'2024-08-01'）。"""
    y, m, d = roc_str.split("/")
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"


def _recent_month_starts(months_back: int) -> list[str]:
    """回傳從本月往前 months_back 個月的 'YYYYMM01' 字串清單，由舊到新排序。"""
    today = date.today()
    y, m = today.year, today.month
    results = []
    for _ in range(months_back):
        results.append(f"{y}{m:02d}01")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(results))


def fetch_stock_name_map(force_refresh: bool = False) -> dict[str, str]:
    """
    抓「上市股票代號 → 股票名稱」對照表。
    透過 TWSE OpenAPI 一次抓全部上市股票的當日資訊，只取代號與名稱兩欄，
    有做簡單的記憶體快取，同一次程式執行預設只打一次 API（除非 force_refresh=True）。
    """
    global _stock_name_cache
    if _stock_name_cache and not force_refresh:
        return _stock_name_cache

    resp = requests.get(
        TWSE_STOCK_DAY_ALL_URL,
        timeout=10,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    payload = resp.json()

    name_map = {row["Code"]: row["Name"] for row in payload if row.get("Code") and row.get("Name")}
    _stock_name_cache = name_map
    return name_map


def get_stock_name(stock_no: str) -> str:
    """查詢股票代號對應的名稱；查不到（例如上櫃股、代號打錯）就回傳空字串。"""
    try:
        return fetch_stock_name_map().get(stock_no, "")
    except Exception:
        return ""


def fetch_twse_month(
    stock_no: str,
    yyyymm01: str,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """
    呼叫 TWSE STOCK_DAY API 抓單一月份的日K，回傳標準化後的 OHLCV DataFrame
    （欄位: Date/Open/High/Low/Close/Volume）。查無資料時回傳空 DataFrame。
    """
    sess = session or requests
    resp = sess.get(
        TWSE_STOCK_DAY_URL,
        params={"response": "json", "date": yyyymm01, "stockNo": stock_no},
        timeout=10,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    payload = resp.json()

    empty = pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    if payload.get("stat") != "OK" or not payload.get("data"):
        return empty

    rows = []
    for r in payload["data"]:
        # TWSE 欄位順序: 日期, 成交股數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, 漲跌價差, 成交筆數
        date_str, vol, _amount, o, h, l, c, *_ = r
        rows.append({
            "Date": pd.to_datetime(_roc_date_to_iso(date_str), errors="coerce"),
            "Open": pd.to_numeric(str(o).replace(",", ""), errors="coerce"),
            "High": pd.to_numeric(str(h).replace(",", ""), errors="coerce"),
            "Low": pd.to_numeric(str(l).replace(",", ""), errors="coerce"),
            "Close": pd.to_numeric(str(c).replace(",", ""), errors="coerce"),
            "Volume": pd.to_numeric(str(vol).replace(",", ""), errors="coerce"),
        })
    df = pd.DataFrame(rows)
    return df.dropna(subset=["Date", "Open", "High", "Low", "Close", "Volume"])


def fetch_twse_ohlcv(stock_no: str, months_back: int = 7, pause_sec: float = 0.3) -> pd.DataFrame:
    """
    抓某檔上市股票最近 months_back 個月的日K，合併成一份 OHLCV DataFrame。
    預設抓 7 個月，扣掉假日大約可湊到 120+ 個交易日，滿足 60MA 計算需求。
    """
    session = requests.Session()
    dfs = []
    for ym in _recent_month_starts(months_back):
        df_month = fetch_twse_month(stock_no, ym, session=session)
        if not df_month.empty:
            dfs.append(df_month)
        time.sleep(pause_sec)  # 避免短時間內打太多次被 TWSE 擋

    if not dfs:
        raise ValueError(
            f"股票代號 {stock_no} 抓不到任何資料，"
            "請確認代號是否正確、是否為上市股票（本函式不支援上櫃）"
        )

    result = pd.concat(dfs, ignore_index=True)
    result = result.drop_duplicates(subset="Date").sort_values("Date").reset_index(drop=True)
    return result


def fetch_and_screen(
    stock_nos: list[str],
    months_back: int = 7,
    params: SOPParams = DEFAULT_PARAMS,
) -> pd.DataFrame:
    """
    只給股票代號清單，自動抓資料 + 跑完整套 SOP 篩選，回傳結果 DataFrame。
    單檔抓取失敗（代號錯誤/查無資料/上櫃股）不會中斷整批，只會印警告並跳過。

        from twse_fetcher import fetch_and_screen
        result = fetch_and_screen(["2330", "2317", "2603"])
        print(result.to_string(index=False))
    """
    data = {}
    for stock_no in stock_nos:
        try:
            data[stock_no] = fetch_twse_ohlcv(stock_no, months_back=months_back)
        except Exception as e:
            print(f"[警告] {stock_no} 抓取失敗，已跳過：{e}")

    if not data:
        raise ValueError("所有股票代號都抓取失敗，沒有資料可供篩選")

    result = screen_stocks(data, params=params)
    result.insert(1, "name", result["symbol"].map(get_stock_name).fillna(""))
    return result


if __name__ == "__main__":
    # 範例：直接對三檔上市股票跑一次抓資料 + SOP 篩選
    result = fetch_and_screen(["2330", "2317", "2603"])
    print(result.to_string(index=False))
