# 股票選股與建倉紀律表 — Python 篩選腳本

把「股票選股與建倉紀律表」轉成可執行的篩選邏輯，輸入 OHLCV 股價資料，
一次掃出符合三道選股過濾的候選股，並可檢查突破後是否出現有效回測日、
計算停損價。

## 檔案說明

- `sop_screener.py`：核心邏輯，所有函式與可調參數都在這裡
- `example_usage.py`：用合成資料跑一遍完整流程的範例，可直接執行 `python3 example_usage.py` 查看效果
- `twse_fetcher.py`：台股資料擷取層，串接台灣證交所（TWSE）公開 API，只要給股票代號
  就能自動抓歷史日K並直接跑完整套 SOP 篩選，不用自己準備 CSV（僅支援上市股票）
- `global_fetcher.py`：美股／港股資料擷取層，透過 yfinance 抓歷史日K，用法與
  `twse_fetcher.py` 對稱
- `stock_screener_app.py`：Streamlit 網頁圖形介面，輸入股票代號、按按鈕即可看到
  篩選結果表格、逐檔詳細檢查、K線走勢圖、停損試算，不用寫程式碼

## 三步驟對照表

| 紀律表項目 | 對應函式 |
|---|---|
| 第一關卡 - 選股三過濾 | `check_stage1_filters(df)` |
| 　1. 均線多頭排列 | `check_trend_up(df)` |
| 　2. 量縮沉澱 | `check_volume_consolidation(df)` |
| 　3. 底底高 | `check_higher_lows(df)` |
| 第二關卡 - 有效回測日四條件 | `check_valid_pullback(df, breakout_idx)` |
| 　→ 明確判斷「今天」買進時機是否確立 | `check_buy_signal_today(df)` |
| 第三關卡 - 停損價計算 | `calc_stop_loss(entry_price, structure_low)` |
| 批次掃描多檔股票 | `screen_stocks({symbol: df, ...})` |
| 只給代號、自動抓資料+批次掃描 | `twse_fetcher.fetch_and_screen([symbol, ...])` |

所有數字門檻（20MA/60MA 天數、量縮比例、停損%、回測時間窗等）
都集中在檔案最上方的 `SOPParams` dataclass，之後要調整紀律表數字，
只要改這裡的參數，不用動邏輯本身。

## 資料格式

每檔股票一個 `pandas.DataFrame`，需含欄位（大小寫、中英文皆可自動辨識）：

```
Date, Open, High, Low, Close, Volume
```

- 建議至少 120 個交易日以上的歷史資料（才能算出穩定的季線）
- CSV 檔可直接用 `load_ohlcv_csv("你的檔案.csv")` 讀取

## 快速上手

```python
from sop_screener import screen_stocks, load_ohlcv_csv

data = {
    "2330": load_ohlcv_csv("2330.csv"),
    "2317": load_ohlcv_csv("2317.csv"),
    "2103": load_ohlcv_csv("2103.csv"),
}

result = screen_stocks(data)
print(result.to_string(index=False))
# 只看通過第一關卡三過濾的候選股
print(result[result["stage1_pass"]])
```

## 更快速上手：只輸入股票代號（免準備 CSV）

不想自己抓資料的話，直接用 `twse_fetcher.py`，它會自動呼叫 TWSE 公開 API
抓歷史日K再送進 `screen_stocks()`：

```bash
pip install requests   # 唯一新增的套件依賴，其餘已含在 pandas/numpy 環境中
```

```python
from twse_fetcher import fetch_and_screen

result = fetch_and_screen(["2330", "2317", "2603"])
print(result.to_string(index=False))
```

也可以直接在終端機跑：

```bash
python twse_fetcher.py
```

**限制**：目前只支援「上市」股票（TWSE STOCK_DAY API），上櫃（TPEx）股票需要另一組
API，尚未實作。每次執行都會即時對 TWSE 發請求（無本地快取），若要頻繁重跑同一批
股票，建議自行把抓回來的 DataFrame 存成 CSV 快取，避免重複打 API。

## 如何清楚確認「買進時機」已經確立（不是模糊判斷）

`check_valid_pullback()` 本身是**回顧式**的：它只回答「突破後1~N天的窗口內，
曾不曾出現過」有效回測日，不會區分那天是今天、還是已經過去的某一天——如果
只看 `valid_pullback_found`，可能會誤把「三天前的舊訊號」當成「現在還能進場」
的理由，等於已經錯過時機才進場。

要精準回答「今天算不算數」，用專門為此設計的 `check_buy_signal_today(df)`：

```python
from sop_screener import check_buy_signal_today

result = check_buy_signal_today(df)
print(result["confirmed"])   # True 才代表：依紀律表，今天可以進場
print(result["reason"])      # 白話說明為什麼確立 / 為什麼還沒確立
```

它會依序排除三種「還沒確立」的情況，只有全部通過才回傳 `confirmed=True`：

1. **尚未偵測到突破日** → 還在整理階段，不用急
2. **今天就是突破日本身** → 回測還沒發生，先觀察不要追高
3. **距突破日已超過 `pullback_window_days`（預設5個交易日）** → 已錯過這次進場
   時機，不能拿窗口內任何一天的舊訊號當作「現在」還能進場的理由
4. 以上都過關後，才看**今天這一天**是否同時符合有效回測日四項條件（量縮、
   價格守穩、K棒小實體、收在上半部）

Streamlit 圖形介面（見下方）已經把這個判斷放在最顯眼的位置：篩選結果最上方
會直接列出「今天買進時機確立」的股票清單，結果表格也有獨立的「今日買進訊號」
欄位，每檔股票展開後還能看到今天逐項條件的檢查明細。

## 美股／港股（用 yfinance）

```bash
pip install yfinance   # 已確認本機裝有 1.5.1
```

```python
from global_fetcher import fetch_and_screen_us, fetch_and_screen_hk

us_result = fetch_and_screen_us(["AAPL", "TSLA", "NVDA"])
hk_result = fetch_and_screen_hk(["0700", "9988", "3690"])  # 騰訊/阿里/美團
```

**代號格式**：
- 美股直接用代號本身，如 `AAPL`（大小寫皆可）
- 港股可輸入 `700` 或 `0700`，會自動補成 yfinance 需要的 `0700.HK` 格式

**注意事項**：
- 資料來源是 Yahoo Finance（非官方 API），穩定性與資料完整度不像 TWSE 官方
  API 有保證，遇到抓取失敗可重試
- 股票名稱查詢（`get_yf_name`）需要額外的網路請求，比 TWSE 那邊的單次批次
  查詢慢一些，已做記憶體快取避免重複查

## 圖形介面（不寫程式也能用）

```bash
pip install streamlit   # 已確認本機裝有 1.59.1
streamlit run stock_screener_app.py
```

會自動開啟瀏覽器頁面（預設 `http://localhost:8501`），介面提供：

- **市場選擇**：台股 (TWSE) / 美股 (US) / 港股 (HK)，切換後代號輸入框的提示文字
  會跟著換
- 股票代號輸入框（多檔用逗號分隔）+「開始篩選」按鈕
- 側邊欄可調整所有 `SOPParams` 參數（月線/季線天數、量縮比例、回測窗口、停損%…），
  不用改程式碼
- 結果表格：代號右側會自動附上股票名稱（透過 TWSE OpenAPI 查詢），一眼看出
  哪些股票通過三過濾、是否已出現有效回測日、**今天買進時機是否確立**（最上方
  另有一行明確列出今天確立的股票代號+名稱）
- 每檔股票可展開看詳細過濾細節（JSON）、回測日逐日檢查表、收盤價走勢圖
- 底部附獨立的停損價試算小工具

限制與 `twse_fetcher.py` 相同（僅上市股票、無本地快取）。要關閉網頁伺服器，回到
終端機按 `Ctrl+C` 即可。

## 如何接入你既有的 Claude Code 型態辨識/回測系統

這份腳本跟你既有系統是**互補**、不是取代關係：

1. **資料層共用**：你現有系統應該已經有一套抓 OHLCV 資料的流程（證交所 API / 資料庫 / CSV），
   把同一份資料 DataFrame 直接餵給這裡的 `screen_stocks()` 即可，不用重複寫資料抓取邏輯。

2. **突破日判斷可以互換**：`find_breakout_day()` 目前只是一個簡化版的突破偵測（創新高+爆量），
   建議之後直接改成呼叫你既有系統裡 W底/整理突破的偵測結果，把該突破日的 index 傳入
   `check_valid_pullback(df, breakout_idx)`，判斷準確度會比這裡的簡化版更好。

3. **建議的整合流程**：
   - 你既有系統：抓資料 → 型態辨識（找出 W底等型態）→ 回測驗證型態歷史勝率
   - 這份腳本：對「型態辨識系統標記出來的候選股」，再套一層「均線多頭 + 量縮 + 底底高」
     的即時篩選，並在訊號出現當下用「有效回測日」四條件把關進場時機，最後套用停損公式
   - 兩者可以串成一個 pipeline：型態辨識找出候選 → SOP 篩選把關進場時機 → 回測系統統計這套流程的歷史勝率

4. **實務上建議把 `SOPParams` 也存成設定檔（json/yaml）**，方便之後在既有系統的 config
   管理架構下統一調整參數，不用改程式碼。

## 注意事項

- `find_breakout_day()` 是簡化版突破偵測，正式使用前建議接上你既有系統更精準的型態判斷
- `check_higher_lows()` 用簡單的 swing low（左右三天內最低）抓局部低點，個股噪音大時
  可以調整 `swing_window` 參數
- 所有函式都回傳詳細的 dict（不是只有 True/False），方便你逐項檢查是哪個條件沒過，
  對照紀律表逐一核對
