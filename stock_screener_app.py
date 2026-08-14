"""
選股紀律 SOP 篩選 —— Streamlit 圖形介面
==========================================
把 sop_screener.py（篩選邏輯）+ twse_fetcher.py（台股）/ global_fetcher.py
（美股、港股）包成網頁介面。選擇市場、輸入股票代號，按下按鈕即自動抓資料並
跑完整套 SOP 篩選，結果用表格呈現，並可展開查看每檔股票的詳細過濾細節、
回測日逐日檢查、K線走勢。

【啟動方式】
    pip install streamlit yfinance   （如尚未安裝，本機已確認裝好）
    streamlit run stock_screener_app.py
會自動開啟瀏覽器頁面（預設 http://localhost:8501）。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from sop_screener import (
    SOPParams,
    add_indicators,
    check_buy_signal_today,
    check_stage1_filters,
    check_valid_pullback,
    find_breakout_day,
)
from twse_fetcher import fetch_twse_ohlcv, get_stock_name as get_tw_name
from global_fetcher import (
    fetch_yf_ohlcv,
    get_yf_name,
    get_yf_name_error,
    normalize_hk_ticker,
    normalize_us_ticker,
)

st.set_page_config(page_title="選股紀律 SOP 篩選", page_icon="📈", layout="wide")

st.title("📈 選股紀律 SOP 篩選")
st.caption("均線多頭排列 + 量縮沉澱 + 底底高 → 三過濾候選股，並檢查有效回測日與停損價")

# ============================================================
# 市場設定：不同市場的代號正規化、抓資料、查名稱方式不同，統一包成一份設定
# ============================================================
MARKETS = {
    "台股 (TWSE，僅上市)": {
        "placeholder": "例如：2330, 2317, 2603",
        "normalize": lambda s: s.strip(),
        "fetch": lambda code, months: fetch_twse_ohlcv(code, months_back=months),
        "name": get_tw_name,
        "name_error": lambda code: "",
    },
    "美股 (US)": {
        "placeholder": "例如：AAPL, TSLA, NVDA",
        "normalize": normalize_us_ticker,
        "fetch": lambda code, months: fetch_yf_ohlcv(code, months_back=months),
        "name": get_yf_name,
        "name_error": get_yf_name_error,
    },
    "港股 (HK)": {
        "placeholder": "例如：700, 9988, 3690（會自動補成 0700.HK 格式）",
        "normalize": normalize_hk_ticker,
        "fetch": lambda code, months: fetch_yf_ohlcv(code, months_back=months),
        "name": get_yf_name,
        "name_error": get_yf_name_error,
    },
}

# ============================================================
# 側邊欄：可調參數（對應 SOPParams，不用改程式碼即可調整紀律表數字）
# ============================================================
with st.sidebar:
    st.header("⚙️ 篩選參數")
    months_back = st.slider(
        "抓取歷史月數", min_value=4, max_value=12, value=7,
        help="至少建議7個月，才能湊出算60MA需要的120+交易日",
    )

    st.subheader("第一關卡：三過濾")
    ma_fast = st.number_input("月線天數 (MA_fast)", value=20, min_value=5, max_value=60)
    ma_slow = st.number_input("季線天數 (MA_slow)", value=60, min_value=20, max_value=120)
    volume_shrink_ratio = st.slider("量縮比例（相對爆量高點）", 0.1, 0.8, 1 / 3, step=0.05)
    consolidation_min_days = st.number_input("最少整理天數", value=15, min_value=5, max_value=60)
    higher_lows_min_count = st.number_input("底底高最少次數", value=2, min_value=1, max_value=5)

    st.subheader("第二關卡：有效回測日")
    pullback_window_days = st.number_input("突破後回測窗口(天)", value=5, min_value=1, max_value=20)

    st.subheader("第三關卡：停損")
    stop_loss_pct = st.slider("百分比停損 (%)", 3, 15, 7) / 100

    params = SOPParams(
        ma_fast=ma_fast,
        ma_slow=ma_slow,
        volume_shrink_ratio=volume_shrink_ratio,
        consolidation_min_days=consolidation_min_days,
        higher_lows_min_count=higher_lows_min_count,
        pullback_window_days=pullback_window_days,
        stop_loss_pct=stop_loss_pct,
    )

# ============================================================
# 主畫面：輸入區
# ============================================================
market_label = st.radio("市場", list(MARKETS.keys()), horizontal=True)
market = MARKETS[market_label]

codes_input = st.text_input(
    "股票代號（多檔用逗號分隔）",
    value="",
    placeholder=market["placeholder"],
)
run = st.button("🔍 開始篩選", type="primary")

if "screen_result" not in st.session_state:
    st.session_state.screen_result = None
    st.session_state.raw_data = {}
    st.session_state.name_fn = market["name"]
    st.session_state.name_error_fn = market["name_error"]

if run:
    stock_nos = [s.strip() for s in codes_input.split(",") if s.strip()]
    if not stock_nos:
        st.warning("請至少輸入一個股票代號")
    else:
        st.session_state.name_fn = market["name"]
        st.session_state.name_error_fn = market["name_error"]
        progress = st.progress(0.0, text="準備開始抓取...")
        data: dict[str, pd.DataFrame] = {}
        errors: dict[str, str] = {}
        for i, raw_code in enumerate(stock_nos):
            stock_no = market["normalize"](raw_code)
            progress.progress(i / len(stock_nos), text=f"抓取 {stock_no} 中...")
            try:
                data[stock_no] = market["fetch"](stock_no, months_back)
            except Exception as e:
                errors[stock_no] = str(e)
        progress.progress(1.0, text="完成")
        progress.empty()

        for stock_no, msg in errors.items():
            st.error(f"{stock_no} 抓取失敗：{msg}")

        st.session_state.raw_data = data

        rows = []
        for symbol, df in data.items():
            stage1 = check_stage1_filters(df, params)
            row = {
                "代號": symbol,
                "名稱": market["name"](symbol),
                "第一關卡通過": stage1["stage1_pass"],
                "均線多頭": stage1["filter1_trend_up"]["pass"],
                "量縮沉澱": stage1["filter2_volume_consolidation"]["pass"],
                "底底高": stage1["filter3_higher_lows"]["pass"],
                "收盤價": stage1["filter1_trend_up"].get("close"),
                "MA20": stage1["filter1_trend_up"].get("ma_fast"),
                "MA60": stage1["filter1_trend_up"].get("ma_slow"),
            }
            df_ind = add_indicators(df, params)
            breakout_idx = find_breakout_day(df_ind)
            if breakout_idx is not None:
                pullback = check_valid_pullback(df_ind, breakout_idx, params)
                row["突破日"] = pullback["breakout_date"]
                row["有效回測"] = pullback["valid_pullback_found"]
            else:
                row["突破日"] = None
                row["有效回測"] = None

            buy_signal = check_buy_signal_today(df, params)
            row["今日買進訊號"] = buy_signal["confirmed"]
            row["_buy_signal_reason"] = buy_signal["reason"]
            rows.append(row)

        result_df = pd.DataFrame(rows)
        if not result_df.empty:
            result_df = result_df.sort_values("第一關卡通過", ascending=False).reset_index(drop=True)
        st.session_state.screen_result = result_df

# ============================================================
# 結果顯示
# ============================================================
if st.session_state.screen_result is not None and not st.session_state.screen_result.empty:
    result_df = st.session_state.screen_result

    # ---- 名稱查詢診斷：名稱空白時直接顯示失敗原因，不用翻雲端 log ----
    name_errors = {
        row["代號"]: st.session_state.name_error_fn(row["代號"])
        for _, row in result_df.iterrows()
        if not row["名稱"]
    }
    name_errors = {k: v for k, v in name_errors.items() if v}
    if name_errors:
        with st.expander("⚠️ 部分股票名稱查詢失敗，點此看原因", expanded=True):
            for code, err in name_errors.items():
                st.text(f"{code}：{err}")

    # ---- 今日買進訊號：最需要一眼看到的結論，獨立列在最上面 ----
    confirmed_today = result_df[result_df["今日買進訊號"]]
    if not confirmed_today.empty:
        labels = [
            f"{row['代號']} {row['名稱']}".strip() for _, row in confirmed_today.iterrows()
        ]
        st.success("🎯 今天買進時機確立：" + "、".join(labels), icon="🎯")
    else:
        st.info("目前沒有任何股票在「今天」符合買進時機確立的條件（詳見下方逐檔原因）")

    st.subheader("篩選結果")

    display_df = result_df.drop(columns=["_buy_signal_reason"]).copy()
    bool_map = {True: "✅", False: "❌"}
    for col in ["第一關卡通過", "均線多頭", "量縮沉澱", "底底高", "今日買進訊號"]:
        display_df[col] = display_df[col].map(bool_map)
    display_df["有效回測"] = display_df["有效回測"].map(bool_map).fillna("—")
    display_df["突破日"] = display_df["突破日"].fillna("—")
    # 欄位順序：代號、名稱緊接在旁，接著是最需要一眼看到的「今日買進訊號」，其餘當作佐證細節
    front_cols = ["代號", "名稱", "今日買進訊號"]
    cols = front_cols + [c for c in display_df.columns if c not in front_cols]
    display_df = display_df[cols]

    st.dataframe(display_df, use_container_width=True, hide_index=True)

    passed = result_df[result_df["第一關卡通過"]]
    st.metric("通過三過濾的候選股", f"{len(passed)} / {len(result_df)}")
    st.metric("今天買進時機確立", f"{len(confirmed_today)} / {len(result_df)}")

    st.subheader("📋 逐檔詳細檢查")
    for symbol in result_df["代號"]:
        df = st.session_state.raw_data.get(symbol)
        if df is None:
            continue
        buy_signal = check_buy_signal_today(df, params)
        stock_label = f"{symbol} {st.session_state.name_fn(symbol)}".strip()
        label = f"{'🎯 ' if buy_signal['confirmed'] else ''}{stock_label} 詳細資料"
        with st.expander(label, expanded=buy_signal["confirmed"]):
            if buy_signal["confirmed"]:
                st.success(f"買進時機確立：{buy_signal['reason']}", icon="🎯")
            else:
                st.warning(f"尚未確立：{buy_signal['reason']}", icon="⏳")
            if buy_signal.get("today_detail"):
                st.caption("今天逐項條件檢查：")
                st.json(buy_signal["today_detail"])

            stage1 = check_stage1_filters(df, params)
            col1, col2, col3 = st.columns(3)
            col1.metric("① 均線多頭", "✅ 通過" if stage1["filter1_trend_up"]["pass"] else "❌ 未通過")
            col2.metric("② 量縮沉澱", "✅ 通過" if stage1["filter2_volume_consolidation"]["pass"] else "❌ 未通過")
            col3.metric("③ 底底高", "✅ 通過" if stage1["filter3_higher_lows"]["pass"] else "❌ 未通過")

            st.json({
                "filter1_trend_up": stage1["filter1_trend_up"],
                "filter2_volume_consolidation": stage1["filter2_volume_consolidation"],
                "filter3_higher_lows": stage1["filter3_higher_lows"],
            })

            df_ind = add_indicators(df, params)
            breakout_idx = find_breakout_day(df_ind)
            if breakout_idx is not None:
                pullback = check_valid_pullback(df_ind, breakout_idx, params)
                found = pullback["valid_pullback_found"]
                st.markdown(
                    f"**突破日**：{pullback['breakout_date']} ｜ "
                    f"**有效回測**：{'✅ 找到' if found else '❌ 未找到'}"
                )
                st.dataframe(
                    pd.DataFrame(pullback["daily_detail"]),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("未偵測到明確突破日（可能仍在整理中，尚未突破）")

            st.caption("收盤價走勢")
            st.line_chart(df.set_index("Date")[["Close"]])
elif st.session_state.screen_result is not None:
    st.warning("沒有任何股票成功抓到資料，請確認代號是否正確")
else:
    st.info("輸入股票代號後按下「開始篩選」即可查看結果")

# ============================================================
# 停損試算（獨立小工具，不需先跑篩選）
# ============================================================
st.divider()
st.subheader("🧮 停損價試算")
c1, c2, c3 = st.columns(3)
entry_price = c1.number_input("進場價", min_value=0.0, value=28.0, step=0.1)
structure_low = c2.number_input("結構低點（回測K棒最低點/前平台低點）", min_value=0.0, value=26.8, step=0.1)
if c3.button("計算停損價"):
    from sop_screener import calc_stop_loss
    stop = calc_stop_loss(entry_price=entry_price, structure_low=structure_low, params=params)
    st.json(stop)
