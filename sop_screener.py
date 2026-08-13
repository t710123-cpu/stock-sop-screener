"""
股票選股與建倉紀律表 —— Python 篩選腳本
=========================================

把「股票選股與建倉紀律表.md」的三道選股過濾 + 有效回測判斷 + 停損計算
轉成可執行的篩選邏輯。輸入標準 OHLCV 股價資料，輸出符合條件的候選股清單。

【輸入資料格式】
每檔股票一個 pandas.DataFrame，欄位需包含（大小寫不拘，會自動轉換）：
    Date, Open, High, Low, Close, Volume
    - Date 需可被 pd.to_datetime 解析
    - 資料需按日期由舊到新排序（腳本內部會自動排序，但仍建議先排好）
    - 建議至少提供 120 個交易日以上的資料，才能算出穩定的 60MA

多檔股票以 dict 形式傳入：{"2330": df_2330, "2317": df_2317, ...}
若你的資料來源是 CSV 檔案，可用 load_ohlcv_csv() 讀取，或自行用
pandas 讀成上述格式後直接呼叫 screen_stocks()。

【與既有型態辨識/回測系統的關係】
本腳本是「選股 + 進場時機 + 停損」邏輯，跟型態辨識系統（W底/M頭/頭肩等）
是不同模組。可以先用本腳本篩出「均線多頭 + 量縮打底 + 底底高」的候選股，
再把候選清單丟進既有的型態辨識系統做更細的型態分類與歷史回測比對。

【使用方式】
    from sop_screener import screen_stocks, load_ohlcv_csv

    data = {
        "2330": load_ohlcv_csv("2330.csv"),
        "2317": load_ohlcv_csv("2317.csv"),
    }
    results = screen_stocks(data)
    print(results.to_string(index=False))
"""

from __future__ import annotations

import dataclasses
from typing import Optional

import numpy as np
import pandas as pd


# ============================================================
# 參數設定區 —— 對應紀律表裡的每一個數字，全部集中在這裡方便調整
# ============================================================

@dataclasses.dataclass
class SOPParams:
    # ---- 第一關卡：選股三過濾 ----
    ma_fast: int = 20                  # 月線
    ma_slow: int = 60                  # 季線
    ma_rising_lookback: int = 5        # 判斷「均線是否上揚」回看幾天前的均線值
    consolidation_min_days: int = 15   # 3~4 週以上整理，以交易日計（約 3週*5=15天）
    volume_shrink_ratio: float = 1 / 3  # 量縮至爆量高點的 1/3 以下
    volume_lookback_days: int = 60     # 找「爆量高點」時往前看幾天
    higher_lows_min_count: int = 2     # 至少幾次回測低點依序墊高
    swing_window: int = 3              # 判斷 swing low 用左右各幾天

    # ---- 第二關卡：有效回測日（四項需同時符合）----
    pullback_window_days: int = 5      # 突破後 1~5 個交易日內的拉回才算數
    pullback_volume_vs_breakout: float = 0.5   # 回測量 / 突破量 < 0.5
    # 回測量 < 20日均量 → 直接沿用 ma_fast 當作均量天數
    pullback_body_ratio_max: float = 0.5       # K棒實體 / 當日振幅，越小代表越像十字線/小實體

    # ---- 第三關卡：停損 ----
    stop_loss_pct: float = 0.07        # 百分比停損 -7%
    intraday_hard_stop_pct: float = 0.10  # 盤中 -10% 先減碼一半

    # ---- 時間停損 ----
    time_stop_days_min: int = 8
    time_stop_days_max: int = 10


DEFAULT_PARAMS = SOPParams()


# ============================================================
# 資料讀取與欄位標準化
# ============================================================

def load_ohlcv_csv(path: str, date_col: str = "Date") -> pd.DataFrame:
    """讀取 CSV 並標準化欄位名稱、日期排序。"""
    df = pd.read_csv(path)
    return _standardize_columns(df, date_col=date_col)


def _standardize_columns(df: pd.DataFrame, date_col: str = "Date") -> pd.DataFrame:
    df = df.copy()
    # 欄位名稱統一轉成標準命名（不區分大小寫比對）
    rename_map = {}
    for col in df.columns:
        low = col.strip().lower()
        if low in ("date", "日期"):
            rename_map[col] = "Date"
        elif low in ("open", "開", "開盤價"):
            rename_map[col] = "Open"
        elif low in ("high", "高", "最高價"):
            rename_map[col] = "High"
        elif low in ("low", "低", "最低價"):
            rename_map[col] = "Low"
        elif low in ("close", "收", "收盤價"):
            rename_map[col] = "Close"
        elif low in ("volume", "量", "成交量"):
            rename_map[col] = "Volume"
    df = df.rename(columns=rename_map)

    required = {"Date", "Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"缺少必要欄位: {missing}，目前欄位為 {list(df.columns)}")

    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"]).reset_index(drop=True)
    return df


# ============================================================
# 指標計算
# ============================================================

def add_indicators(df: pd.DataFrame, params: SOPParams = DEFAULT_PARAMS) -> pd.DataFrame:
    """附加均線、均量等技術指標欄位。"""
    df = df.copy()
    df["MA_fast"] = df["Close"].rolling(params.ma_fast).mean()
    df["MA_slow"] = df["Close"].rolling(params.ma_slow).mean()
    df["Vol_MA_fast"] = df["Volume"].rolling(params.ma_fast).mean()
    return df


# ============================================================
# 第一關卡：選股三過濾
# ============================================================

def check_trend_up(df: pd.DataFrame, params: SOPParams = DEFAULT_PARAMS) -> dict:
    """
    項目1：大結構趨勢向上
    標準：20MA 與 60MA 都上揚，且股價站在 20MA 之上
    """
    if len(df) < params.ma_slow + params.ma_rising_lookback:
        return {"pass": False, "reason": "資料天數不足以計算季線趨勢"}

    last = df.iloc[-1]
    prev = df.iloc[-1 - params.ma_rising_lookback]

    ma_fast_rising = last["MA_fast"] > prev["MA_fast"]
    ma_slow_rising = last["MA_slow"] > prev["MA_slow"]
    price_above_fast = last["Close"] > last["MA_fast"]

    passed = bool(ma_fast_rising and ma_slow_rising and price_above_fast)
    return {
        "pass": passed,
        "ma_fast_rising": bool(ma_fast_rising),
        "ma_slow_rising": bool(ma_slow_rising),
        "price_above_ma_fast": bool(price_above_fast),
        "close": round(float(last["Close"]), 2),
        "ma_fast": round(float(last["MA_fast"]), 2),
        "ma_slow": round(float(last["MA_slow"]), 2),
    }


def check_volume_consolidation(df: pd.DataFrame, params: SOPParams = DEFAULT_PARAMS) -> dict:
    """
    項目2：籌碼經歷量縮沉澱
    標準：橫盤整理 3~4 週以上（>= consolidation_min_days 個交易日），
         且近期量能已縮至過去 volume_lookback_days 天內爆量高點的 1/3 以下
    """
    lb = params.volume_lookback_days
    if len(df) < lb:
        return {"pass": False, "reason": "資料天數不足以判斷量縮"}

    window = df.iloc[-lb:]
    peak_volume = window["Volume"].max()
    peak_date = window.loc[window["Volume"].idxmax(), "Date"]

    recent = df.iloc[-params.consolidation_min_days:]
    recent_avg_volume = recent["Volume"].mean()
    recent_max_volume = recent["Volume"].max()

    shrink_threshold = peak_volume * params.volume_shrink_ratio
    # 用「近期整理區間的最大量」而非平均量來卡，避免單一均量掩蓋還有暴量殘留
    passed = bool(recent_max_volume <= shrink_threshold)

    return {
        "pass": passed,
        "peak_volume": int(peak_volume),
        "peak_date": str(pd.Timestamp(peak_date).date()),
        "recent_max_volume": int(recent_max_volume),
        "recent_avg_volume": round(float(recent_avg_volume), 0),
        "shrink_threshold(peak*1/3)": round(float(shrink_threshold), 0),
    }


def _find_swing_lows(df: pd.DataFrame, window: int) -> list[tuple[int, float, pd.Timestamp]]:
    """找出局部低點（左右 window 天內都比它高）。回傳 (index, low_price, date) 清單。"""
    lows = df["Low"].values
    swing_lows = []
    for i in range(window, len(df) - window):
        seg = lows[i - window: i + window + 1]
        if lows[i] == seg.min() and np.argmin(seg) == window:
            swing_lows.append((i, float(lows[i]), df["Date"].iloc[i]))
    return swing_lows


def check_higher_lows(df: pd.DataFrame, params: SOPParams = DEFAULT_PARAMS) -> dict:
    """
    項目3：底底高型態成立
    標準：整理期間至少 higher_lows_min_count 次回測，低點一次比一次高
    只在最近 consolidation_min_days*2 天的區間內找 swing low，避免抓到太久以前的低點
    """
    lookback = params.consolidation_min_days * 2
    recent_df = df.iloc[-lookback:].reset_index(drop=True) if len(df) > lookback else df.reset_index(drop=True)

    swing_lows = _find_swing_lows(recent_df, params.swing_window)
    if len(swing_lows) < params.higher_lows_min_count:
        return {
            "pass": False,
            "reason": f"只找到 {len(swing_lows)} 個 swing low，不足 {params.higher_lows_min_count} 個",
            "swing_lows": [(str(pd.Timestamp(d).date()), p) for _, p, d in swing_lows],
        }

    prices = [p for _, p, _ in swing_lows]
    is_ascending = all(prices[i] < prices[i + 1] for i in range(len(prices) - 1))

    return {
        "pass": bool(is_ascending),
        "swing_lows": [(str(pd.Timestamp(d).date()), round(p, 2)) for _, p, d in swing_lows],
    }


def check_stage1_filters(df: pd.DataFrame, params: SOPParams = DEFAULT_PARAMS) -> dict:
    """跑完第一關卡的三項過濾，回傳總結果。"""
    df_ind = add_indicators(df, params)
    r1 = check_trend_up(df_ind, params)
    r2 = check_volume_consolidation(df_ind, params)
    r3 = check_higher_lows(df_ind, params)

    all_pass = bool(r1.get("pass") and r2.get("pass") and r3.get("pass"))
    return {
        "stage1_pass": all_pass,
        "filter1_trend_up": r1,
        "filter2_volume_consolidation": r2,
        "filter3_higher_lows": r3,
    }


# ============================================================
# 第二關卡：有效回測日判斷
# ============================================================

def find_breakout_day(df: pd.DataFrame, lookback: int = 60) -> Optional[int]:
    """
    簡易突破日定義：最近 lookback 天內，收盤價創新高、且量能為近 20 日最大量的那一天。
    回傳該天在 df 中的 index；找不到則回傳 None。
    這是一個簡化版本，實務上你可能想接你現有型態辨識系統輸出的突破點，
    直接把該 index 傳給 check_valid_pullback() 取代這個函式。
    """
    window = df.iloc[-lookback:] if len(df) > lookback else df
    window = window.reset_index(drop=True)
    rolling_high = window["Close"].cummax()
    vol_ma20 = window["Volume"].rolling(20).mean()

    candidates = window[
        (window["Close"] >= rolling_high * 0.999) &
        (window["Volume"] > vol_ma20 * 1.5)
    ]
    if candidates.empty:
        return None
    local_idx = candidates.index[-1]
    offset = len(df) - len(window)
    return int(local_idx + offset)


def check_valid_pullback(
    df: pd.DataFrame,
    breakout_idx: int,
    params: SOPParams = DEFAULT_PARAMS,
) -> dict:
    """
    對照紀律表「有效回測日」四項標準，逐日檢查突破後 1~pullback_window_days 天內
    是否出現符合條件的回測日。四項全數符合才算「有效回測」。

    breakout_idx: 突破日在 df 中的整數位置索引
    """
    df_ind = add_indicators(df, params)
    breakout_row = df_ind.iloc[breakout_idx]
    breakout_volume = breakout_row["Volume"]
    # 突破基準點：前整理區上緣壓力線用「突破日前 20 天最高收盤價」近似，或 20MA，取較高者
    pre_breakout_window = df_ind.iloc[max(0, breakout_idx - 20):breakout_idx]
    resistance_level = pre_breakout_window["Close"].max() if not pre_breakout_window.empty else breakout_row["MA_fast"]
    base_line = max(resistance_level, breakout_row["MA_fast"])

    end_idx = min(len(df_ind), breakout_idx + 1 + params.pullback_window_days)
    window = df_ind.iloc[breakout_idx + 1: end_idx]

    results = []
    valid_found = False
    for i, row in window.iterrows():
        day_range = row["High"] - row["Low"]
        body = abs(row["Close"] - row["Open"])
        body_ratio = body / day_range if day_range > 0 else 0
        close_position = (row["Close"] - row["Low"]) / day_range if day_range > 0 else np.nan

        cond_volume_vs_breakout = row["Volume"] < breakout_volume * params.pullback_volume_vs_breakout
        cond_volume_vs_avg = row["Volume"] < row["Vol_MA_fast"] if not np.isnan(row["Vol_MA_fast"]) else False
        cond_price = row["Close"] >= base_line
        cond_small_body = body_ratio <= params.pullback_body_ratio_max
        cond_upper_half = close_position >= 0.5 if not np.isnan(close_position) else False

        day_valid = bool(
            cond_volume_vs_breakout and cond_volume_vs_avg and cond_price
            and cond_small_body and cond_upper_half
        )
        if day_valid:
            valid_found = True

        results.append({
            "date": str(pd.Timestamp(row["Date"]).date()),
            "close": round(float(row["Close"]), 2),
            "volume": int(row["Volume"]),
            "cond_volume_vs_breakout(<50%)": bool(cond_volume_vs_breakout),
            "cond_volume_vs_20MA": bool(cond_volume_vs_avg),
            "cond_close_above_base(%.2f)" % base_line: bool(cond_price),
            "cond_small_body": bool(cond_small_body),
            "cond_close_upper_half": bool(cond_upper_half),
            "valid_pullback_day": day_valid,
        })

    return {
        "breakout_date": str(pd.Timestamp(breakout_row["Date"]).date()),
        "breakout_volume": int(breakout_volume),
        "base_line": round(float(base_line), 2),
        "valid_pullback_found": valid_found,
        "daily_detail": results,
    }


def check_buy_signal_today(df: pd.DataFrame, params: SOPParams = DEFAULT_PARAMS) -> dict:
    """
    明確判斷「以資料最後一天（今天）為基準，買進時機是否確立」。

    跟 check_valid_pullback() 的差別：
    - check_valid_pullback() 是回顧式的，只回答「突破後1~N天的窗口內，
      曾不曾出現過」有效回測日，不會區分那天是今天還是已經過去的某天。
    - 這個函式專門回答「今天算不算數」：
        1. 有沒有偵測到突破日
        2. 距今天是否還在有效回測窗口內（超過窗口 = 已錯過這次進場機會，
           不能拿窗口內任何一天的舊訊號當作「現在」還能進場的理由）
        3. 今天這一天本身，是否同時符合有效回測日四項條件

    confirmed=True 才代表：依照紀律表，今天是可以進場的一天。
    """
    df_ind = add_indicators(df, params)
    breakout_idx = find_breakout_day(df_ind)
    latest_idx = len(df_ind) - 1

    if breakout_idx is None:
        return {
            "confirmed": False,
            "reason": "尚未偵測到突破日，仍在整理階段，不用急著進場",
            "breakout_date": None,
        }

    if breakout_idx == latest_idx:
        return {
            "confirmed": False,
            "reason": "今天就是突破日本身，回測還沒發生，先觀察不要追高",
            "breakout_date": str(pd.Timestamp(df_ind.iloc[breakout_idx]["Date"]).date()),
        }

    days_since_breakout = latest_idx - breakout_idx
    if days_since_breakout > params.pullback_window_days:
        return {
            "confirmed": False,
            "reason": (
                f"距突破日已過 {days_since_breakout} 個交易日，"
                f"超過有效回測窗口（{params.pullback_window_days}天），已錯過這次進場時機"
            ),
            "breakout_date": str(pd.Timestamp(df_ind.iloc[breakout_idx]["Date"]).date()),
            "days_since_breakout": days_since_breakout,
        }

    pullback = check_valid_pullback(df_ind, breakout_idx, params)
    today_detail = pullback["daily_detail"][-1] if pullback["daily_detail"] else None
    today_valid = bool(today_detail and today_detail["valid_pullback_day"])

    return {
        "confirmed": today_valid,
        "reason": "今天同時符合有效回測日四項條件，買進時機確立" if today_valid
        else "今天尚未同時符合四項回測條件，再等等，不要提早進場",
        "breakout_date": pullback["breakout_date"],
        "today_detail": today_detail,
        "days_since_breakout": days_since_breakout,
        "days_remaining_in_window": params.pullback_window_days - days_since_breakout,
    }


# ============================================================
# 第三關卡：停損價計算
# ============================================================

def calc_stop_loss(
    entry_price: float,
    structure_low: float,
    params: SOPParams = DEFAULT_PARAMS,
) -> dict:
    """
    紀律表停損邏輯：
        實際停損價 = 結構價（回測K棒最低點/前平台低點）與 (-7%) 兩者中，離進場價「較近」者
    """
    pct_stop_price = entry_price * (1 - params.stop_loss_pct)
    # 取離進場價較近者 = 價格較高的那一個（因為兩者都在進場價之下）
    stop_price = max(structure_low, pct_stop_price)
    stop_type = "structure" if stop_price == structure_low else "percentage(-7%)"

    intraday_hard_stop_price = entry_price * (1 - params.intraday_hard_stop_pct)

    return {
        "entry_price": round(entry_price, 2),
        "structure_stop_price": round(structure_low, 2),
        "percentage_stop_price": round(pct_stop_price, 2),
        "final_stop_price": round(stop_price, 2),
        "final_stop_type": stop_type,
        "final_stop_pct": round((stop_price / entry_price - 1) * 100, 2),
        "intraday_hard_stop_price(-10%, 減碼一半)": round(intraday_hard_stop_price, 2),
    }


# ============================================================
# 整合：一次跑完整套 SOP 篩選（多檔股票）
# ============================================================

def screen_stocks(
    data: dict[str, pd.DataFrame],
    params: SOPParams = DEFAULT_PARAMS,
    check_pullback: bool = True,
) -> pd.DataFrame:
    """
    對多檔股票跑第一關卡三過濾（+ 選擇性檢查是否已有有效回測日），
    回傳整理好的 DataFrame，方便一次掃出候選股清單。
    """
    rows = []
    for symbol, df in data.items():
        try:
            df_std = _standardize_columns(df) if "Date" not in df.columns or df["Date"].dtype == object else df.copy()
            df_std["Date"] = pd.to_datetime(df_std["Date"])
            df_std = df_std.sort_values("Date").reset_index(drop=True)

            stage1 = check_stage1_filters(df_std, params)
            row = {
                "symbol": symbol,
                "stage1_pass": stage1["stage1_pass"],
                "filter1_trend_up": stage1["filter1_trend_up"]["pass"],
                "filter2_volume_consolidation": stage1["filter2_volume_consolidation"]["pass"],
                "filter3_higher_lows": stage1["filter3_higher_lows"]["pass"],
                "close": stage1["filter1_trend_up"].get("close"),
                "ma20": stage1["filter1_trend_up"].get("ma_fast"),
                "ma60": stage1["filter1_trend_up"].get("ma_slow"),
            }

            if check_pullback and stage1["stage1_pass"]:
                df_ind = add_indicators(df_std, params)
                breakout_idx = find_breakout_day(df_ind)
                if breakout_idx is not None:
                    pullback = check_valid_pullback(df_ind, breakout_idx, params)
                    row["breakout_date"] = pullback["breakout_date"]
                    row["valid_pullback_found"] = pullback["valid_pullback_found"]
                else:
                    row["breakout_date"] = None
                    row["valid_pullback_found"] = None

            rows.append(row)
        except Exception as e:
            rows.append({"symbol": symbol, "stage1_pass": False, "error": str(e)})

    result_df = pd.DataFrame(rows)
    sort_cols = [c for c in ["stage1_pass"] if c in result_df.columns]
    if sort_cols:
        result_df = result_df.sort_values(sort_cols, ascending=False).reset_index(drop=True)
    return result_df


# ============================================================
# 範例用法（可直接執行本檔測試流程是否正確）
# ============================================================

if __name__ == "__main__":
    print(__doc__)
    print("此檔案僅定義函式，請參考 example_usage.py 產生範例資料並實際跑一次篩選。")
