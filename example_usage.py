"""
範例用法：用合成資料測試 sop_screener.py 的完整篩選流程。

實際使用時，把下方 make_synthetic_stock() 換成你真正的股價資料來源
（例如你既有系統已經在用的證交所 API / CSV / 資料庫），
資料格式只要整理成含 Date, Open, High, Low, Close, Volume 欄位的
DataFrame 即可直接餵給 screen_stocks()。
"""

import numpy as np
import pandas as pd

from sop_screener import (
    SOPParams,
    add_indicators,
    check_stage1_filters,
    check_valid_pullback,
    calc_stop_loss,
    find_breakout_day,
    screen_stocks,
)


def make_synthetic_stock(seed: int, pattern: str = "good_setup") -> pd.DataFrame:
    """
    產生一檔合成股票資料，用來測試篩選邏輯。
    pattern="good_setup": 模擬「均線多頭 + 量縮打底 + 底底高 + 量縮回測」的理想案例
    pattern="chasing_high": 模擬「剛爆量噴出、沒有回測」的案例（應該被排除或標記為尚無有效回測）
    """
    rng = np.random.default_rng(seed)
    n = 140
    dates = pd.bdate_range("2026-01-01", periods=n)

    if pattern == "consolidating_now":
        # 前段急拉爆量,中後段橫盤打底且持續量縮到現在(尚未突破) → 應通過三過濾,適合列入觀察
        base = np.concatenate([
            np.linspace(18, 24, 40) + rng.normal(0, 0.3, 40),           # 前段急拉
            24 + np.cumsum(rng.normal(0, 0.08, 100)) * 0.2,             # 長期橫盤打底(底底高)
        ])
        close = base[:n]
        volume = np.concatenate([
            rng.integers(25000, 32000, 40),     # 前段爆量
            rng.integers(3000, 6000, 100),      # 持續量縮打底到現在
        ])[:n]
    elif pattern == "good_setup":
        # 前段緩漲建立季線,中段橫盤打底且量縮,末段量縮突破+量縮回測
        base = np.concatenate([
            np.linspace(20, 24, 60) + rng.normal(0, 0.3, 60),          # 前段緩漲
            24 + np.cumsum(rng.normal(0, 0.15, 50)) * 0.3,             # 中段橫盤打底(底底高)
            np.linspace(24, 24, 20),                                    # 暫存,下面覆寫末段
        ])
        # 末段:量縮突破 + 拉回
        breakout = np.array([24.2, 25.5, 26.8, 27.5, 27.0, 26.6, 26.9, 27.3, 27.6, 27.8,
                              27.9, 28.0, 27.7, 27.5, 27.8, 28.1, 28.3, 28.5, 28.6, 28.8])
        base = np.concatenate([base[:120], breakout])
        close = base

        volume = np.concatenate([
            rng.integers(8000, 12000, 60),
            rng.integers(4000, 7000, 50),      # 打底期量縮
            [rng.integers(30000, 35000)],      # 突破爆量
            rng.integers(6000, 9000, 9),       # 突破後量縮回測
            rng.integers(9000, 13000, 20),     # 後續補到與 close 等長
        ])
    else:  # chasing_high: 剛爆量噴出,沒有回測
        base = np.concatenate([
            20 + np.cumsum(rng.normal(0, 0.1, 100)) * 0.2,
            np.linspace(21, 21.5, 30),
        ])
        close = np.concatenate([base[:130], [22, 24, 27, 29, 29.5, 30.5, 31, 31.5, 32, 32.5]])
        volume = np.concatenate([
            rng.integers(3000, 6000, 130),
            [8000, 15000, 35000, 40000, 38000, 36000, 33000, 30000, 28000, 25000],
        ])

    close = close[:n]
    volume = volume[:n]

    open_ = close - rng.normal(0, 0.15, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.15, 0.1, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.15, 0.1, n))

    return pd.DataFrame({
        "Date": dates,
        "Open": open_,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": volume,
    })


def main():
    params = SOPParams()

    data = {
        "STOCK_A_consolidating_now": make_synthetic_stock(seed=1, pattern="consolidating_now"),
        "STOCK_B_chasing_high": make_synthetic_stock(seed=2, pattern="chasing_high"),
        "STOCK_C_breakout_with_pullback": make_synthetic_stock(seed=3, pattern="good_setup"),
    }

    print("=" * 60)
    print("Step 1：批次篩選（第一關卡三過濾 + 是否已有有效回測）")
    print("=" * 60)
    result = screen_stocks(data, params=params)
    print(result.to_string(index=False))

    print("\n" + "=" * 60)
    print("Step 2：對通過第一關卡的股票，看詳細的回測日逐日檢查")
    print("=" * 60)
    for symbol, df in data.items():
        df_ind = add_indicators(df, params)
        stage1 = check_stage1_filters(df, params)
        print(f"\n--- {symbol} ---")
        print("filter1 大結構趨勢向上:", stage1["filter1_trend_up"])
        print("filter2 量縮沉澱:", stage1["filter2_volume_consolidation"])
        print("filter3 底底高:", stage1["filter3_higher_lows"])

        # 回測日判斷獨立於第一關卡結果，只要能找到突破日就能檢查
        # (實務上你會用「通過第一關卡的股票」持續追蹤到突破發生為止)
        breakout_idx = find_breakout_day(df_ind)
        if breakout_idx is not None:
            pullback = check_valid_pullback(df_ind, breakout_idx, params)
            print("突破日:", pullback["breakout_date"], "| 是否找到有效回測日:", pullback["valid_pullback_found"])
        else:
            print("未偵測到明確突破日（可能仍在整理，尚未突破）")

        if not stage1["stage1_pass"]:
            print("（註：未通過第一關卡三過濾，正式流程中不會列入候選觀察名單）")

    print("\n" + "=" * 60)
    print("Step 3：停損價計算範例")
    print("=" * 60)
    stop = calc_stop_loss(entry_price=28.0, structure_low=26.8, params=params)
    for k, v in stop.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
