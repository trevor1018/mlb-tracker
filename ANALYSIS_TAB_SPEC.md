# 「分析」Tab 開發規格 (v4 — 球場因素+天氣)

## 資料來源

Python 腳本 `mlb_analyzer.py` 產出 `mlb_analysis.json`，web app 讀取此 JSON 渲染 UI。

執行方式：
```bash
cd D:/python/mlb-tracker

# 每日例行 (今天)
python -X utf8 mlb_analyzer.py daily

# 指定特定日期 (支援多種格式)
python -X utf8 mlb_analyzer.py daily 4.11       # 2026-04-11
python -X utf8 mlb_analyzer.py daily 4/11       # 2026-04-11
python -X utf8 mlb_analyzer.py daily 2025-09-28 # 歷史日期

# 不抓新資料, 只重新產生分析 (快)
python -X utf8 mlb_analyzer.py today 4.11
```

輸出檔案：`D:/python/mlb-tracker/mlb_analysis.json`

> **重要**：程式會自動使用 target_date **之前**的比賽建立 tracker，避免用未來資料 (lookahead bias)。
> 意即 `daily 4.11` 用的是 4/10 收盤時的球隊/投手累積數據去預測 4/11 比賽。

> **v3 重大更新**：支援 6 種盤口（不讓分、讓分 1.5、讓分 2.5、大小分 7.5/8.5/9.5），每種盤口有獨立的指標排名、bucket thresholds 和 profitable filters。

> **v4 重大更新**：新增球場因素 (Park Factor)、球場元資料 (venue)、即時天氣 (Open-Meteo)。Park factor 是大小分最強指標，所有 `+pf` 複合指標衝到排名前 4。

---

## v4 vs v3 的差異

| 項目 | v3 | v4 |
|------|----|----|
| 球場資訊 | 無 | **venue: {name, roof, elevation, park_factor}** |
| 天氣資料 | 無 | **weather: {temp_f, humidity, wind_mph, wind_dir_deg, precip_prob}** |
| 大小分指標 | 25 個複合 | **29 個** (+4 含 park factor: `+pf`) |
| est_total | runs_per_game + SP修正 | **+ park_factor 修正** |
| 大小分 Top 1 | ops+sp_fip (53.3%) | **ops+fip+pf (55.0%)** |
| 天氣指標 | 無 | park_factor 54.3%, temp_f 53.0%, humidity 52.7% |
| 正 EV 篩選 (大小分) | 7-10 個 | **28-31 個** (大幅增加) |

---

## 10 年回測關鍵發現 (供 UI 文案)

| 盤口 | 最強指標 | Top 10% 命中率 | 損益平衡賠率 | 正 EV 篩選數 |
|------|---------|--------------|------------|-------------|
| 🥇 **不讓分** | `comp:run_diff+fip` | **67.9%** | 2.17 | 125 |
| 🥈 **讓分 1.5** | `comp:ops+whip` | 65.6% | 2.28 | 82 |
| 🥉 **讓分 2.5** | `comp:ops+whip` | 63.5% | 2.48 | 60 |
| 4 **大小分 7.5** | `comp:ops+fip+pf` | **63.6%** | 2.47 | 31 |
| 5 **大小分 8.5** | `comp:ops+fip+pf` | **63.6%** | 2.47 | 28 |
| 6 **大小分 9.5** | `comp:ops+sp_fip+pf` | **65.0%** | 2.38 | 30 |

### 重要警告 (要在 UI 顯示)

⚠️ **讓分盤口已自動排除 +home 複合變體**
- 主場優勢對讓分 1.5 只有 36.5%, 對讓分 2.5 只有 27.2% (都是強烈反向)
- 原因：主場隊常常只贏 1 分，不夠 cover 讓分
- 程式已自動跳過，無需 UI 處理

⚠️ **bp_fatigue 只在大小分 9.5 有效**
- 其他盤口都是 50% (無效)
- 大小分 9.5 Top 10% 達 60.3%

---

## JSON Schema (v3)

```json
{
  "generated_at": "2026-04-11T16:30:00",
  "date": "2026-04-11",
  "current_season": 2026,
  "current_season_games": 192,

  "baseline": {
    "season_range": "2015-2025",
    "total_games_analyzed": 23950,
    "bet_types": {
      "ml": {
        "name": "不讓分",
        "type": "directional",
        "line": 0,
        "indicator_ranking": [
          {
            "name": "composite:run_diff+sp_fip+home",
            "pct": 56.7,
            "correct": 10127,
            "total": 17868,
            "z_score": 17.9,
            "significant": true,
            "direction": "複合",
            "category": "composite"
          }
          // ... 46 個指標
        ],
        "profitable_filters": [
          {
            "stat": "comp:pyth+ops+sp_fip+home",
            "top_pct": 10,
            "sample": 1786,
            "single_pct": 66.9,
            "parlay_pct": 44.7,
            "breakeven_combined_odds": 2.24,
            "threshold": 1.8234
          }
          // ... 101 個
        ]
      },
      "spread_1.5": {
        "name": "讓分 1.5",
        "type": "directional",
        "line": 1.5,
        "indicator_ranking": [...],
        "profitable_filters": [...]  // 65 個
      },
      "spread_2.5": { ... },
      "total_7.5": { ... },
      "total_8.5": { ... },
      "total_9.5": { ... }
    }
  },

  "today_matchups": [
    {
      "away": "Atlanta Braves",
      "away_zh": "勇士",
      "home": "Los Angeles Angels",
      "home_zh": "天使",
      "away_sp": "Grant Holmes",
      "home_sp": "Reid Detmers",
      "away_record": "8-5",
      "home_record": "6-7",

      "away_sp_recent": [
        {"date": "2026-04-10", "vs": "Team", "ip": "6.0", "er": 2, "k": 7, "bb": 1, "result": "W 5-2"}
      ],
      "home_sp_recent": [...],

      "venue": {
        "id": 1,
        "name": "Angel Stadium",
        "roof": "Open",           // "Open" | "Retractable" | "Dome"
        "elevation": 151,
        "park_factor": 0.996      // > 1.0 = 打者球場 (偏大分), < 1.0 = 投手球場 (偏小分)
      },

      "weather": {                // null if unavailable
        "temp_f": 72.5,           // 華氏溫度
        "humidity": 45,           // 濕度 %
        "wind_mph": 8.3,          // 風速 mph
        "wind_dir_deg": 210,      // 風向 (0=北, 90=東, 180=南, 270=西)
        "precip_prob": 5          // 降雨機率 % (forecast only)
      },

      "team_comparisons": {
        "ops":              { "away": 0.721, "home": 0.698, "edge": "away" },
        "era":              { "away": 3.45,  "home": 4.10,  "edge": "away" },
        "fip":              { "away": 3.80,  "home": 4.25,  "edge": "away" },
        "whip":             { "away": 1.22,  "home": 1.35,  "edge": "away" },
        "k9":               { "away": 8.5,   "home": 7.2,   "edge": "away" },
        "run_diff_per_game": { "away": 0.8,  "home": -0.3,  "edge": "away" },
        "pyth_pct":         { "away": 0.560, "home": 0.470, "edge": "away" }
      },

      "sp_comparisons": {
        "sp_starts":  { "away": 3,    "home": 3,    "edge": "even" },
        "sp_era":     { "away": 2.85, "home": 5.20, "edge": "away" },
        "sp_fip":     { "away": 3.10, "home": 4.85, "edge": "away" },
        "sp_whip":    { "away": 1.05, "home": 1.42, "edge": "away" },
        "sp_k9":      { "away": 9.8,  "home": 6.5,  "edge": "away" },
        "sp_bb9":     { "away": 2.1,  "home": 4.3,  "edge": "away" },
        "sp_k_bb":    { "away": 4.67, "home": 1.51, "edge": "away" }
      },

      "away_edges": 10,
      "home_edges": 0,
      "any_top_10_pct": true,

      "bet_types": {
        "ml": {
          "name": "不讓分",
          "line": 0,
          "composite_scores": {
            "comp:pyth+ops+sp_fip+home": {
              "value": 1.85,
              "predicts": "away",
              "bucket": "Top 10%",
              "baseline_top_10_hit_rate": 66.9
            }
            // ... 19 個 directional composites
          },
          "matched_filters": [
            {
              "stat": "comp:pyth+ops+sp_fip+home",
              "top_pct": 10,
              "baseline_hit_rate": 66.9,
              "min_odds_needed": 2.24
            }
          ],
          "is_top_10_pct": true,
          "predicted": "away",
          "votes": { "home": 2, "away": 17 },
          "confidence": "high",
          "min_combined_odds_needed": 2.24
        },

        "spread_1.5": {
          "name": "讓分 1.5",
          "line": 1.5,
          "composite_scores": {
            "comp:pyth+ops+sp_fip": {
              "value": 1.62,
              "predicts": "away",
              "bucket": "Top 10%",
              "baseline_top_10_hit_rate": 65.3
            }
            // 注意: 沒有 +home 變體 (已排除)
          },
          "matched_filters": [...],
          "is_top_10_pct": true,
          "predicted": "away",
          "votes": { "home": 1, "away": 11 },
          "confidence": "high",
          "min_combined_odds_needed": 2.34
        },

        "spread_2.5": { ... },

        "total_7.5": {
          "name": "大小分 7.5",
          "line": 7.5,
          "composite_scores": {
            "comp:ops+fip+pf": {
              "value": 3.10,
              "predicts": "over",
              "bucket": "Top 10%",
              "baseline_top_10_hit_rate": 63.6
            },
            "comp:runs+era+pf": {
              "value": 2.33,
              "predicts": "over",
              "bucket": "Top 20%",
              "baseline_top_10_hit_rate": 64.2
            }
            // ... 29 個 total composites (含 4 個 +pf)
          },
          "matched_filters": [
            {
              "stat": "comp:ops+fip+pf",
              "top_pct": 10,
              "baseline_hit_rate": 63.6,
              "min_odds_needed": 2.47
            }
          ],
          "is_top_10_pct": true,
          "predicted": "over",
          "votes": {
            "est_total": 9.33,
            "line": 7.5,
            "distance": 1.83,
            "over_count": 11,
            "under_count": 4,
            "over_z_sum": 27.81,
            "under_z_sum": 6.38,
            "minority_ratio": 0.19,
            "conflict": false
          },
          "confidence": "medium",
          "signal_conflict": false,
          "min_combined_odds_needed": 2.47
        },

        "total_8.5": { ... },
        "total_9.5": { ... }
      }
    }
  ]
}
```

---

## UI 設計規格

### Tab 加入方式

在現有 tabs 陣列新增：
```js
{id: "analysis", icon: "🔬", label: "分析"}
```

### AnalysisView 子頁籤結構

**三個子頁**（用現有的 `Pill` 元件）：
1. **今日分析** (預設) — 顯示今日每場比賽的 6 盤口預測
2. **指標驗證** — 顯示 6 盤口各自的指標排名
3. **可獲利篩選** — 顯示 6 盤口各自的 profitable filters 清單

---

### 子頁 1：今日分析

每場比賽一張大卡片。排序規則：
1. 任一盤口符合 Top 10% (`any_top_10_pct: true`) 的場次優先
2. 其餘依 `abs(away_edges - home_edges)` 降序

#### 卡片結構

```
┌──────────────────────────────────────────────────────────┐
│ ⭐ 符合 Top 10% (僅 any_top_10_pct=true 顯示 banner)    │
├──────────────────────────────────────────────────────────┤
│ 勇士 (8-5)       @       天使 (6-7)                     │
│ Grant Holmes     vs      Reid Detmers                    │
│                                                          │
│ 【球場 & 天氣】                                          │
│   🏟️ Angel Stadium (Open)  PF=0.996                      │
│   🌡️ 72°F  💨 8mph  💧 45%  ☔ 5%                        │
│                                                          │
│ 【今日先發投手對決】                                     │
│   sp_fip   3.10 ◀ 4.85   ✓ 勇士優                       │
│   sp_era   2.85 ◀ 5.20   ✓ 勇士優                       │
│   sp_k9    9.8  ◀ 6.5    ✓ 勇士優                       │
│   sp_whip  1.05 ◀ 1.42   ✓ 勇士優                       │
│                                                          │
│ 【球隊整季數據】                                          │
│   OPS      .721 ◀ .698   ✓ 勇士優                       │
│   ERA      3.45 ◀ 4.10   ✓ 勇士優                       │
│   pyth%    .560 ◀ .470   ✓ 勇士優                       │
│                                                          │
│ 【六盤口預測】 (Tab 切換顯示)                            │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ [不讓分] [讓分 1.5] [讓分 2.5] [7.5] [8.5] [9.5]    │ │
│ ├─────────────────────────────────────────────────────┤ │
│ │ 不讓分 ⭐ Top 10%                                    │ │
│ │ 預測: 勇士                    信心度: 高 ⭐⭐⭐       │ │
│ │ 複合分數投票: 勇士 17 / 天使 2                      │ │
│ │                                                      │ │
│ │ 符合篩選:                                            │ │
│ │  ✅ comp:pyth+ops+sp_fip+home Top 10%               │ │
│ │     歷史命中 66.9%, 最低合計賠率 2.24               │ │
│ │  ✅ comp:run_diff+sp_fip+home Top 10%               │ │
│ │     歷史命中 66.3%, 最低合計賠率 2.28               │ │
│ │                                                      │ │
│ │ 主要複合分數:                                        │ │
│ │   comp:run_diff+sp_fip+home   +1.92 → 勇士 ⭐       │ │
│ │   comp:pyth+ops+sp_fip+home   +1.85 → 勇士 ⭐       │ │
│ │   comp:ops+sp_fip+home        +1.62 → 勇士          │ │
│ └─────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

#### 盤口 Tab 切換邏輯

卡片內部用**子 Tab** 切換顯示不同盤口的預測：
- Tab 標籤: `不讓分 ⭐` (⭐ 代表符合 Top 10%) / `讓分 1.5` / `讓分 2.5` / `7.5` / `8.5` / `9.5`
- 點擊後顯示該盤口的 `predicted`, `votes`, `confidence`, `matched_filters`, 主要 `composite_scores`

#### 狀態顏色

| 狀態 | 顏色 | 使用時機 |
|------|------|---------|
| 深綠 | `#10b981` | `is_top_10_pct: true` (該盤口符合 Top 10%) |
| 淺綠 | `#6ee7b7` | `confidence: high` 但非 Top 10% |
| 黃色 | `#fbbf24` | `confidence: medium` |
| 灰色 | `#6b7280` | `confidence: low` 或 `predicted: even` |

---

### 子頁 2：指標驗證

顯示 6 盤口的指標排名，用 **盤口 Pill 切換**。

```
┌──────────────────────────────────────────────────────────┐
│ [不讓分] [讓分 1.5] [讓分 2.5] [大 7.5] [大 8.5] [大 9.5] │
├──────────────────────────────────────────────────────────┤
│  排名  圖示  指標名                      命中率    樣本  │
│  ────  ────  ─────────────────────────  ────────  ──────│
│   1   🥇   comp:run_diff+sp_fip+home     56.7%    17868 │
│   2   🥈   comp:run_diff+sp_fip          56.6%    17868 │
│   3   🥉   comp:pyth+ops+sp_fip+home     56.6%    17868 │
│   ...                                                     │
│  45   ⚠️   home_adv  (讓分反向!)          -- 隱藏 --     │
│  46   ⚠️   bp_fatigue  (10年驗證無效)     50.0%    17255 │
└──────────────────────────────────────────────────────────┘
```

#### 顏色規則

| 命中率範圍 | 顏色 | 註記 |
|-----------|------|------|
| ≥ 56% | 深綠 | 強信號 |
| 54-56% | 淺綠 | 有效 |
| 52-54% | 灰 | 微弱 |
| 50-52% | 淡黃 | 持平 |
| < 50% | 紅 | ⚠️ 反向 |

#### 特別標註

- **讓分 1.5/2.5 頁面**：home_adv 仍顯示但**標記「反向指標，已自動排除」**
- **大小分頁面**：bp_fatigue 在 9.5 顯示正常（60.3%），在其他頁面標記「僅 9.5 有效」
- **category 標籤**：
  - `composite` → 顯示「複合」
  - `starter` → 顯示「先發投手」
  - `team` → 顯示「球隊累積」
  - `bullpen` → 顯示「牛棚 ⚠️」
  - `situational` → 顯示「情境」

---

### 子頁 3：可獲利篩選

顯示 6 盤口的 profitable filters，用 **盤口 Pill 切換**。

```
┌─────────────────────────────────────────────────────────────────────┐
│ [不讓分: 101] [讓分 1.5: 65] [讓分 2.5: 44] [7.5: 10] [8.5: 8] [9.5: 7]│
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ 💡 使用方法:                                                         │
│ 1. 每天跑 daily 後，檢視「今日分析」找符合這些篩選的場次            │
│ 2. 兩腳合計賠率必須超過「損益平衡賠率」才有正期望值                │
│ 3. Top% 10 的樣本數少，可靠度最高；Top% 30 則較寬鬆                 │
│                                                                     │
│ 排名  指標                          Top%  樣本  命中率  損益平衡賠率 │
│ ────  ──────────────────────────── ────  ────  ──────  ──────────── │
│  1    🥇 comp:pyth+ops+sp_fip+home  10%   1786  66.9%  2.24          │
│  2    comp:pyth+ops+sp_fip          10%   1786  66.6%  2.26          │
│  3    comp:run_diff+sp_fip+home     10%   1786  66.3%  2.28          │
│  4    comp:run_diff+fip             10%   2395  66.1%  2.29          │
│  5    comp:run_diff+fip+home        10%   2395  66.0%  2.29          │
│  ... (可摺疊, 預設顯示前 15)                                         │
└─────────────────────────────────────────────────────────────────────┘
```

#### 頂部說明文字 (必要)

```
📊 數據: 2015-2025 共 10 季 23,950 場 (已排除 2020 COVID 縮短賽季)
🔄 上次更新: {generated_at}

⚠️ 注意:
  • 這些篩選是「歷史回測」命中率, 不保證未來表現
  • 樣本 < 1,000 的篩選可靠度較低
  • 台彩大小分賠率通常低於損益平衡賠率，實戰以 ML 和讓分 1.5 為主
```

---

## 實作注意事項

### 排序與過濾

```js
// 卡片排序
matchups.sort((a, b) => {
  // 1. Top 10% 場次優先
  if (a.any_top_10_pct && !b.any_top_10_pct) return -1;
  if (!a.any_top_10_pct && b.any_top_10_pct) return 1;
  // 2. 依優勢差距降序
  return Math.abs(b.away_edges - b.home_edges) - Math.abs(a.away_edges - a.home_edges);
});

// 取得主要複合分數 (顯示 top 3)
function getTopScores(betType, n=3) {
  return Object.entries(betType.composite_scores)
    .sort((a, b) => Math.abs(b[1].value) - Math.abs(a[1].value))
    .slice(0, n);
}
```

### Bucket 顯示

- `bucket === "Top 10%"` → 顯示 ⭐⭐⭐ 且加粗
- `bucket === "Top 20%"` → 顯示 ⭐⭐
- `bucket === "Top 30%"` → 顯示 ⭐
- `bucket === null` → 不顯示符號

### predicted 顯示轉換

```js
function formatPrediction(bt, awayZh, homeZh) {
  if (bt.predicted === 'home') return homeZh;
  if (bt.predicted === 'away') return awayZh;
  if (bt.predicted === 'over')  return '大';
  if (bt.predicted === 'under') return '小';
  return '—';
}
```

---

## 資料載入方式

### 選項 A：手動貼上 JSON（建議優先做）

```jsx
const [analysisData, setAnalysisData] = useState(null);
const [jsonText, setJsonText] = useState('');

// 載入時:
useEffect(() => {
  const saved = localStorage.getItem('mlb_analysis_json');
  if (saved) setAnalysisData(JSON.parse(saved));
}, []);

// 貼上 + 儲存:
<textarea value={jsonText} onChange={e => setJsonText(e.target.value)} />
<button onClick={() => {
  try {
    const data = JSON.parse(jsonText);
    setAnalysisData(data);
    localStorage.setItem('mlb_analysis_json', jsonText);
  } catch (e) {
    alert('JSON 格式錯誤');
  }
}}>載入分析</button>
```

### 選項 B：從 URL 載入

```js
useEffect(() => {
  fetch('mlb_analysis.json').then(r => r.json()).then(setAnalysisData);
}, []);
```

---

## 相關檔案

- `D:/python/mlb-tracker/mlb_analyzer.py` — 分析引擎
- `D:/python/mlb-tracker/cache/baseline_10y.json` — 10 年 baseline (含 park_factors)
- `D:/python/mlb-tracker/cache/venues.json` — 30 座 MLB 球場元資料 (座標/屋頂/海拔)
- `D:/python/mlb-tracker/cache/weather_cache.json` — 歷史天氣快取 (23,341 場)
- `D:/python/mlb-tracker/mlb_analysis.json` — daily 輸出 (web app 讀取)
- `D:/python/mlb-tracker/MLB_INDICATORS.txt` — 指標完整定義

## mlb_analyzer.py 指令

```bash
correlate range 2015 2025   # 一次性執行: 跑 10 年回測 + 自動存 baseline
daily [date]                 # 每日例行: 抓當季最新 + 產生分析 JSON
today [date]                 # 不抓新資料, 只重新產生分析

# 日期參數支援:
daily            # 今天
daily 4.11       # 2026-04-11
daily 4/11       # 2026-04-11
daily 2025-09-28 # 歷史日期 (跑歷史回放)
```

## 指定日期的用途

- **明天的比賽預測**：`daily 4.12`（比 `daily` 更明確，避免時區混淆）
- **歷史回放分析**：`today 2025-05-15` 看當時的分析會如何
- **補昨天的紀錄**：`daily 4.10` 重算昨天的場次
- **跨時區對應**：台灣下午跑 `daily 4.11`，對應美國時間的 4/11 比賽
