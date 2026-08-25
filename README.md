# ⚾ MLB 投注追蹤 / 資料分析

台灣運彩 MLB 的資料分析工具：用 MLB 官方 API + Baseball Savant 逐球資料，
找出各種盤口的可下注訊號，並且**用嚴格的統計把關，不讓雜訊冒充規律**。

線上版：<https://trevor1018.github.io/mlb-tracker/>
完整分析報告：[ANALYSIS_REPORT.md](ANALYSIS_REPORT.md)

## 目前的結論（2026 球季至 8/24）

| 問題 | 答案 |
|------|------|
| 有沒有命中率 75%+ 的條件？ | 有，但 482 萬個假設裡只剩 **11 組** 過得了四道關卡 |
| 每一隊都能找到最佳條件嗎？ | 每隊都有 100% 命中的組合，但**運氣線也是 100%**；嚴格版本下只有 3 隊勝過運氣 |
| 模型能賺錢嗎？ | 兩階段驗證（連挑策略都不准偷看未來）ROI **−3.3%**；只有大小分 7.5/8.5 撐得住 |
| 對左右投 / 對球種 / 日夜場分項有用嗎？ | 消融實驗顯示：**只有球場與天氣明顯有貢獻**，其餘小於雜訊尺度 |

> 這些「否定」的答案本身就是最有價值的部分 —— 它們把錢從必輸的玩法上省下來。

## 網頁功能

| 頁面 | 內容 |
|------|------|
| 首頁 | 資料概況、Tier A 條件數、待開打強訊號、兩階段驗證 ROI |
| 分析 → 推薦 | 未開打場次的 edge 排行、賠率輸入即算期望值、串關試算、逐場對位速查 |
| 分析 → 回測 | 兩階段驗證、單注 lift 排序、串關模擬（4選3 等） |
| 分析 → 條件 | Tier A/B 條件（含運氣線、四時段命中率）、樣本外檢驗、跨季驗證 |
| 分析 → 球隊 | 各隊對左右投 / 球種 / 日夜 / 主客的 wOBA、xwOBA、K%、強擊率 + 聯盟排名 |
| 分析 → 球員 | 每隊 14 名打者、12 名投手的同款分項 + 投手球種使用率與球速 |
| 分析 → 模型 | 得分期望值模型與各盤口樣本外表現、校準表、特徵族群消融實驗 |
| 維護 | 雲端資料筆數、一鍵清除 Firestore 資料 |

## 分析方法（為什麼數字可信）

1. **不使用賽後資訊**：每場的特徵都是「該場開打前」的累積值（as-of 快照）。
2. **條件挖掘四道關卡**：樣本 ≥40 → BH-FDR 校正 q<0.05 → 命中率高於 permutation
   運氣線（把結果打亂 25 次取最高命中率的 95 百分位）→ 四個時段都不崩。
3. **模型全部樣本外**：每半個月滾動重訓，只預測訓練期之後的比賽。
4. **ROI 用基準率定價**：假設賠率 =（1/該玩法基準命中率）× 返還率，
   避免「押基準率本來就高的盤」產生假優勢。
5. **兩階段驗證**：連「押哪個盤口、用什麼門檻」都只能用前段資料決定。

## 技術架構

### 前端
- React 18 + Babel standalone（單一 `index.html`，CDN 載入，無 build 工具）
- Firebase Auth（Google 登入）+ Firestore（投注紀錄同步）
- 分析資料從 `output/app_data.json`（約 600KB）與 `output/player_splits.json` 讀取
- 部署：GitHub Pages（push 到 main 約 1 分鐘生效）

### 分析管線（`analysis/`）

```
資料抓取
  fetch_season.py       整季賽程 + 逐局比分 + 先發預告（含未開打場次）
  fetch_boxscores.py    每場先發/牛棚/打線/球隊加總
  fetch_people.py       球員左右投打
  fetch_savant.py       Baseball Savant 逐球 Statcast（球種、站位、xwOBA…）

資料整理
  build_gamelogs.py     逐球 → 逐場 × 分項聚合
  build_splits.py       球隊分項（對左右投/球種/日夜/主客）→ output/team_splits.json
  build_player_splits.py 球員分項 → output/player_splits.json
  build_dataset.py      as-of 特徵 + 各玩法結果 → teamgames / gamesds / pending

分析
  predicates.py         特徵離散化成條件（分位數門檻只用訓練期算）
  mine_conditions.py    條件挖掘（apriori 剪枝、Wilson 下界、FDR、permutation 運氣線）
  mine_team_conditions.py 每隊各自挖掘（含該隊運氣線）
  mine_multiseason.py   跨球季挖掘與驗證
  model_markets.py      每個盤口各訓一個分類器（對照組）
  model_runs.py         得分期望值模型（主引擎：Poisson → 負二項 → 卷積）
  ablation.py           特徵族群消融實驗
  backtest.py           期望值回測（含兩階段驗證與串關模擬）
  predict_slate.py      未開打場次的預測與 edge 排行

輸出
  build_app_data.py     壓成網頁用的 app_data.json
  make_report.py        產生 ANALYSIS_REPORT.md
  run_all.py            一鍵跑完整條管線
  daily_update.py       每日增量更新（含快取失效處理，可自動 push）
```

### 資料存放

```
data/{season}/     每季的資料集（gitignore，不進版控）
data/cache/        HTTP 回應快取（gzip），重跑很快
output/            分析結果 JSON（進版控，網頁直接讀）
output/{season}/   過去球季的分析結果
```

## 使用方式

### 每日更新

```bash
cd analysis
python daily_update.py            # 更新到美西昨天
python daily_update.py --push     # 跑完自動 commit + push
python daily_update.py --skip-mining   # 只更新資料與推薦（快）
```

### 從零重建

```bash
cd analysis
python run_all.py                 # 2026 球季全套（第一次約 30 分鐘，多在抓 Savant）
MLB_SEASON=2025 python run_all.py --only fetch_season,fetch_boxscores,fetch_people,fetch_savant,build_gamelogs,build_dataset
python mine_multiseason.py --train 2024,2025 --test 2026
```

### 本地開發

Firebase Auth 不支援 `file://`，需要本地伺服器：

```bash
python -m http.server 8080   # 然後開 http://localhost:8080
```

### 上 code

```bash
git add -A && git commit -m "訊息" && git push
```

推上 `main` 後 GitHub Pages 自動部署，約 1 分鐘生效（瀏覽器記得強制重新載入）。

## 資料來源

| 來源 | 用途 | 需要 key |
|------|------|---------|
| [MLB Stats API](https://statsapi.mlb.com) | 賽程、比分、boxscore、球員資料 | 不用 |
| [Baseball Savant](https://baseballsavant.mlb.com) | 逐球 Statcast（球種、xwOBA、揮棒速度） | 不用 |

2026 球季至 8/24：**1974 場完賽、582,340 球**逐球資料。

## Firestore 資料結構

```
users/{uid}/bets/{betId}     — 投注紀錄
users/{uid}/games/{gameId}   — 賽果紀錄
```

規則見 `firestore.rules`：只允許使用者讀寫自己的資料。

## 已知限制

- **沒有真實賠率**：ROI 都基於「假設賠率 = 公正賠率 × 返還率」。接上真實賠率
  （the-odds-api 等）之後，「模型機率 vs 市場隱含機率」才是真正可下注的 edge。
- **單季樣本**：2000 場、單隊 130 場，條件挖掘的統計力天生不足。
- **模型天花板**：樣本外 AUC 0.55–0.58。職業盤口能到 0.60+ 主要靠市場價格當特徵。
- **先發預告會變**：開賽前應重跑 `predict_slate.py`。

## 授權

MIT License
