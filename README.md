# ⚾ MLB 投注追蹤 / 資料分析

台灣運彩 MLB 的資料分析工具：用 MLB 官方 API + Baseball Savant 逐球資料，
找出各種盤口的可下注訊號，並且**用嚴格的統計把關，不讓雜訊冒充規律**。

線上版：<https://trevor1018.github.io/mlb-tracker/>
完整分析報告：[ANALYSIS_REPORT.md](ANALYSIS_REPORT.md)

## 目前的結論（2026 球季至 8/24）

| 問題 | 答案 |
|------|------|
| 有沒有命中率 75%+ 的條件？ | 763 萬個假設裡只剩 **14 組** 過得了四道關卡。改用三季資料 + lift 標準後，23.8 萬組候選有 **10 組**跨季存活（每季 lift ≥1.11）|
| 每一隊都能找到最佳條件嗎？ | 每隊都有 100% 命中的組合，但**運氣線也是 100%**；嚴格版本下只有 3 隊勝過運氣 |
| 模型能賺錢嗎？ | 兩階段驗證 ROI **−2.6%**（台彩 90% 返還率）。看似有優勢的「全場大分」做球場校正後 ROI 從 +18.3% 變 −0.7% |
| 對左右投 / 對球種 / 日夜場分項有用嗎？ | 消融實驗（6 種子）顯示：**沒有任何單一族群的獨立貢獻超過雜訊 2 倍**（球場天氣 1.8σ 最接近）。但全特徵比只用基本盤好 3.1σ → 資訊是集體性的、彼此高度重疊。而且**只用 16 個「莊家一定知道」的欄位就和 109 欄一樣好** |
| 那分項資料的價值在哪？ | 在「理解比賽」—— 球隊/球員瀏覽、對位速查。不在「打敗盤口」 |

> 這些「否定」的答案本身就是最有價值的部分 —— 它們把錢從必輸的玩法上省下來。

## 網頁功能

| 頁面 | 內容 |
|------|------|
| 首頁 | 資料概況、Tier A 條件數、待開打強訊號、兩階段驗證 ROI |
| 分析 → 推薦 | 逐場總覽（一次一天）：所有玩法的機率/基準/保本、填台彩賠率即算期望值、串關試算、展開看對位分析（含直接對戰史） |
| 分析 → 回測 | 兩階段驗證、單注 lift 排序、串關模擬（4選3 等） |
| 分析 → 條件 | Tier A/B 條件（含運氣線、四時段命中率）、樣本外檢驗、跨季驗證 |
| 分析 → 球隊 | 各隊對左右投 / 球種 / 日夜 / 主客的 wOBA、xwOBA、K%、強擊率 + 聯盟排名 |
| 分析 → 球員 | 每隊 14 名打者、12 名投手的同款分項 + 投手球種使用率與球速 |
| 分析 → 模型 | 得分期望值模型與各盤口樣本外表現、校準表、消融實驗、市場代理測試 |
| 紀錄 | 記錄真實下注（含台彩賠率）、一鍵自動結算、真實 ROI 與模型校準 |
| 維護 | 雲端資料筆數、一鍵清除 Firestore 資料 |

## 分析方法（為什麼數字可信）

1. **不使用賽後資訊**：每場的特徵都是「該場開打前」的累積值（as-of 快照）。
2. **條件挖掘四道關卡**：樣本 ≥40 → BH-FDR 校正 q<0.05 → 命中率高於 permutation
   運氣線（把結果打亂 25 次取最高命中率的 95 百分位）→ 四個時段都不崩。
3. **模型全部樣本外**：每半個月滾動重訓，只預測訓練期之後的比賽。
4. **ROI 用基準率定價**：假設賠率 =（1/該玩法基準命中率）× 返還率，
   避免「押基準率本來就高的盤」產生假優勢。
5. **兩階段驗證**：連「押哪個盤口、用什麼門檻」都只能用前段資料決定。
6. **市場代理對照**：另外訓練一個只用「莊家一定知道的資訊」的模型，
   看完整模型有沒有贏過它（答案：沒有）。
7. **球場校正**：用「該球場自己的歷史大分率」當基準，
   把莊家一定會定價的球場效應扣掉再看優勢。

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
  ablation.py           特徵族群消融實驗（含多種子雜訊尺度）
  market_proxy.py       市場代理測試：完整模型 vs「莊家一定知道的 16 欄」
  over_rule.py          大分規則查表 + 球場校正對照
  backtest.py           期望值回測（含兩階段驗證與串關模擬）
  predict_slate.py      未開打場次的各玩法機率
  build_matchup_report.py 單場對位分析（直接對戰史 + 類型對位，含聚合快取）
  validate_matchup.py   驗證對位分數的預測力（含 as-of 洩漏處理）
  live_update.py        賽前 60/30 分的個別更新（抓實際打線）

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

兩個 Windows 工作排程：

| 排程 | 時間 | 做什麼 |
|------|------|--------|
| `MLB-DailyUpdate` | 每天台灣時間 **00:00** | 全量更新（抓昨天比賽 → 重算所有分析 → push），約 4-6 分鐘 |
| `MLB-LiveUpdate` | 台灣時間 **00:00-10:00 每 30 分鐘**檢查一次 | 有比賽進入賽前 **60 分 / 30 分**窗口才動作：抓實際先發打線與最新先發投手，重算該場對位分析並 push，約 10 秒 |

為什麼需要第二個：先發打線通常賽前 1 小時才公布（實測 MLB API 在 T-40 分就有），
半夜那次只能用「近 30 天推估打線」。賽前更新會換成實際打線，網頁上每場卡片
右上角有時間戳與「實際先發打線 / 近30天推估打線」標籤。

窗口容差設 ±15 分（T-75~T-45 與 T-45~T-15 連續覆蓋），因為掃描間隔是 30 分鐘 ——
容差若只有 ±10 分，掃到 T-45 時兩個窗口都不符，下一次掃已經 T-15 來不及了。

台灣 00:00-10:00 對應美東前一天 12:00-22:00，涵蓋整天的比賽。
唯一例外是台灣時間 10:40 之後才開賽的西岸夜場（很少），
那種場次只會拿到半夜全量的推估打線；要涵蓋的話把持續時間從 10 小時改長即可。

記錄檔：`data/logs/daily_*.log`（14 份）、`data/logs/live_*.log`（7 天）。

```powershell
# 手動立刻跑一次
Start-ScheduledTask -TaskName "MLB-DailyUpdate"
# 看狀態與下次執行時間
Get-ScheduledTaskInfo -TaskName "MLB-DailyUpdate"
# 暫停 / 恢復
Disable-ScheduledTask -TaskName "MLB-DailyUpdate"
Enable-ScheduledTask  -TaskName "MLB-DailyUpdate"
```

排程注意事項：
- 以「使用者已登入」身分執行 —— 電腦關機或登出時不會跑，但開機後會補跑
  （`StartWhenAvailable`）
- 兩個踩過的坑已修好：`.ps1` 要存 UTF-8 with BOM（PS 5.1 才讀得懂中文）、
  要設 `FOR_DISABLE_CONSOLE_CTRL_HANDLER=1`（否則 numpy/scipy 底層的 Intel Fortran
  runtime 會因 console 關閉事件中止，出現 `forrtl: error (200)`）

也可以直接手動跑：

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

- **沒有真實賠率**：ROI 都基於「假設賠率 = 公正賠率 × 返還率」。這個假設會高估優勢 ——
  球場校正就顯示大分的優勢大半來自莊家一定會定價的球場效應。
  **解法已內建**：用「紀錄」tab 記下每筆真實台彩賠率，累積成自己的賠率資料庫。
- **單季樣本**：2000 場、單隊 130 場，條件挖掘的統計力天生不足。
- **模型天花板**：樣本外 AUC 0.55–0.58。職業盤口能到 0.60+ 主要靠市場價格當特徵。
- **先發預告會變**：開賽前應重跑 `predict_slate.py`。

## 授權

MIT License
