# 「分析」Tab 開發規格 (v2)

## 資料來源

Python 腳本 `mlb_analyzer.py` 會產出 `mlb_analysis.json`，web app 讀取此 JSON 渲染 UI。

執行方式（每日例行）：
```bash
cd D:/python/mlb-tracker
python -X utf8 mlb_analyzer.py daily
```

輸出檔案：`D:/python/mlb-tracker/mlb_analysis.json`

> **重要更新**：分析引擎已大幅升級。詳見下方「v2 升級說明」。

---

## v2 升級說明

相較於前一版規格，主要新增：

1. **個別先發投手追蹤** — 不再只用球隊累積投手數據，現在追蹤每位投手的個別 ERA/WHIP/FIP/K9/BB9
2. **主場優勢加成** — 複合指標可疊加 +0.15 z-score 主場 bonus
3. **新複合指標** — 含 `sp_xxx` 和 `+home` 後綴的組合
4. **分層回測 (bucket analysis)** — 找出真正可正 ROI 的場次篩選條件
5. **多季回測** — 支援單季 / 多季 / 範圍合併分析
6. **舊版警示**：早期 2026 數據顯示「牛棚疲勞 60.3%」是統計幻覺，10 年大樣本驗證為 50.0%，實質無效

---

## JSON Schema (v2)

```json
{
  "generated_at": "2026-04-11T16:30:00",
  "date": "2026-04-11",
  "season_range": "2015-2025",
  "total_games_analyzed": 23950,

  "correlations": {
    // === 球隊累積指標 ===
    "win_pct":     { "correct": 13179, "total": 23596, "pct": 55.9, "z_score": 18.0, "significant": true,  "direction": "高者勝" },
    "run_diff_pg": { "correct": 13375, "total": 23917, "pct": 55.9, "z_score": 18.2, "significant": true,  "direction": "高者勝" },
    "pyth_pct":    { "correct": 13339, "total": 23947, "pct": 55.7, "z_score": 17.5, "significant": true,  "direction": "高者勝" },
    "ops":         { "correct": 13074, "total": 23950, "pct": 54.6, "z_score": 14.1, "significant": true,  "direction": "高者勝" },
    "era":         { "correct": 13255, "total": 23947, "pct": 55.4, "z_score": 16.6, "significant": true,  "direction": "低者勝" },
    "whip":        { "correct": 13198, "total": 23947, "pct": 55.1, "z_score": 15.7, "significant": true,  "direction": "低者勝" },
    "fip":         { "correct": 13092, "total": 23948, "pct": 54.7, "z_score": 14.3, "significant": true,  "direction": "低者勝" },
    "bp_fatigue":  { "correct": 8631,  "total": 17250, "pct": 50.0, "z_score": 0.0,  "significant": false, "direction": "低者勝" },
    "home_adv":    { "correct": 12778, "total": 23950, "pct": 53.4, "z_score": 10.5, "significant": true,  "direction": "主場" },

    // === 個別先發投手指標 (新) ===
    "sp_era":      { "correct": 940, "total": 1750, "pct": 53.7, "significant": true, "direction": "低者勝" },
    "sp_whip":     { "correct": 917, "total": 1752, "pct": 52.3, "significant": false, "direction": "低者勝" },
    "sp_fip":      { "correct": 955, "total": 1753, "pct": 54.5, "significant": true, "direction": "低者勝" },
    "sp_k9":       { "correct": 921, "total": 1752, "pct": 52.6, "significant": true, "direction": "高者勝" },
    "sp_k_bb":     { "correct": 940, "total": 1748, "pct": 53.8, "significant": true, "direction": "高者勝" },

    // === 複合指標 (含 sp_ 和 +home 變體) ===
    "composite:run_diff+sp_fip+home":   { "correct": 966, "total": 1753, "pct": 55.1, "significant": true },
    "composite:pyth+ops+sp_fip+home":   { "correct": 979, "total": 1753, "pct": 55.8, "significant": true },
    "composite:ops+sp_fip+home":        { "correct": 966, "total": 1753, "pct": 55.1, "significant": true },
    "composite:ops+era+home":           { "correct": 1306, "total": 2383, "pct": 54.8, "significant": true },
    "composite:ops+era":                { "correct": 1297, "total": 2383, "pct": 54.4, "significant": true }
    // ... 約 20 個複合指標
  },

  "bucket_analysis": {
    "comp:run_diff+sp_fip+home": {
      "buckets": [
        { "range": "Top 20%",   "wins": 308, "total": 476, "pct": 64.8 },
        { "range": "20-40%",    "wins": 270, "total": 476, "pct": 56.7 },
        { "range": "40-60%",    "wins": 250, "total": 476, "pct": 52.5 },
        { "range": "60-80%",    "wins": 240, "total": 476, "pct": 50.4 },
        { "range": "Bottom 20%", "wins": 230, "total": 479, "pct": 48.0 }
      ],
      "top_10_pct": { "wins": 175, "total": 251, "pct": 69.7, "breakeven_odds": 2.06 }
    }
    // ... 其他分層
  },

  "profitable_filters": [
    { "stat": "comp:run_diff+sp_fip+home", "top_pct": 10, "sample": 175, "single_pct": 69.7, "parlay_pct": 48.6, "breakeven_combined_odds": 2.06 },
    { "stat": "comp:pyth+ops+sp_fip+home", "top_pct": 10, "sample": 175, "single_pct": 69.1, "parlay_pct": 47.8, "breakeven_combined_odds": 2.09 },
    { "stat": "era",                       "top_pct": 10, "sample": 238, "single_pct": 67.6, "parlay_pct": 45.8, "breakeven_combined_odds": 2.19 }
    // ... top 15 篩選條件
  ],

  "matchups": [
    {
      "away": "Atlanta Braves",
      "away_zh": "勇士",
      "home": "Los Angeles Angels",
      "home_zh": "天使",
      "away_sp": "Grant Holmes",
      "home_sp": "Reid Detmers",
      "away_record": "8-5",
      "home_record": "6-7",

      "team_comparisons": {
        "ops":              { "away": 0.721, "home": 0.698, "edge": "away" },
        "slg":              { "away": 0.410, "home": 0.389, "edge": "away" },
        "obp":              { "away": 0.311, "home": 0.309, "edge": "away" },
        "era":              { "away": 3.45,  "home": 4.10,  "edge": "away" },
        "whip":             { "away": 1.22,  "home": 1.35,  "edge": "away" },
        "fip":              { "away": 3.80,  "home": 4.25,  "edge": "away" },
        "k9":               { "away": 8.5,   "home": 7.2,   "edge": "away" },
        "run_diff_per_game": { "away": 0.8,  "home": -0.3,  "edge": "away" },
        "pyth_pct":         { "away": 0.560, "home": 0.470, "edge": "away" }
      },

      "sp_comparisons": {
        "sp_starts":  { "away": 3,    "home": 3,    "edge": "even" },
        "sp_era":     { "away": 2.85, "home": 5.20, "edge": "away" },
        "sp_whip":    { "away": 1.05, "home": 1.42, "edge": "away" },
        "sp_fip":     { "away": 3.10, "home": 4.85, "edge": "away" },
        "sp_k9":      { "away": 9.8,  "home": 6.5,  "edge": "away" },
        "sp_bb9":     { "away": 2.1,  "home": 4.3,  "edge": "away" },
        "sp_k_bb":    { "away": 4.67, "home": 1.51, "edge": "away" }
      },

      "composite_scores": {
        "ops+era":              { "value":  0.85, "predicts": "away" },
        "run_diff+sp_fip+home": { "value":  1.42, "predicts": "away" },
        "pyth+ops+sp_fip+home": { "value":  1.55, "predicts": "away" }
      },

      "bucket_position": {
        "comp:run_diff+sp_fip+home": "Top 10%",
        "comp:pyth+ops+sp_fip+home": "Top 10%",
        "comp:ops+era":              "Top 20%"
      },

      "recommendation": {
        "is_top_10_pct": true,
        "matched_filters": [
          "comp:run_diff+sp_fip+home Top 10%",
          "comp:pyth+ops+sp_fip+home Top 10%"
        ],
        "predicted_winner": "away",
        "predicted_winner_zh": "勇士",
        "confidence": "high",
        "min_combined_odds_needed": 2.06,
        "note": "符合最強篩選條件 (Top 10%)，建議下注"
      },

      "away_edges": 9,
      "home_edges": 0,
      "edge_summary": "away"
    }
    // ... 其他場次
  ]
}
```

---

## UI 設計規格

### Tab 加入方式

在現有 tabs 陣列（約 line 1030）新增：
```js
{id: "analysis", icon: "🔬", label: "分析"}
```

對應渲染（約 line 1050）：
```js
{tab === "analysis" && <AnalysisView />}
```

### AnalysisView 元件結構

**三個子頁籤**（用現有的 `Pill` 元件）：
- `今日分析` (預設)
- `指標驗證`
- `可獲利篩選`

---

#### 子頁 1：今日分析

從 `matchups` 陣列渲染。**重點：把符合 Top 10% 篩選條件的場次標記為「強烈推薦」**

```
排序規則:
  1. is_top_10_pct = true 的場次排最前面 (綠色高亮)
  2. 其餘依 |away_edges - home_edges| 降序

卡片結構:

┌─────────────────────────────────────────────┐
│ ⭐ 強烈推薦  (僅 is_top_10_pct = true 顯示)  │
├─────────────────────────────────────────────┤
│ 勇士 (8-5)  @  天使 (6-7)                  │
│ Grant Holmes  vs  Reid Detmers              │
│                                             │
│ 【今日先發投手對決】 (新)                    │
│   sp_fip   3.10 ◀ 4.85   ✓ 勇士優           │
│   sp_era   2.85 ◀ 5.20   ✓ 勇士優           │
│   sp_k9    9.8  ◀ 6.5    ✓ 勇士優           │
│   sp_whip  1.05 ◀ 1.42   ✓ 勇士優           │
│                                             │
│ 【球隊整季數據】                              │
│   OPS      .721 ◀ .698   ✓ 勇士優           │
│   ERA      3.45 ◀ 4.10   ✓ 勇士優           │
│   WHIP     1.22 ◀ 1.35   ✓ 勇士優           │
│   pyth%    .560 ◀ .470   ✓ 勇士優           │
│                                             │
│ 【複合分數】                                  │
│   ops+era              +0.85 → 勇士          │
│   run_diff+sp_fip+home +1.42 → 勇士 ⭐      │
│   pyth+ops+sp_fip+home +1.55 → 勇士 ⭐      │
│                                             │
│ 【符合篩選】                                  │
│   ✅ comp:run_diff+sp_fip+home Top 10%     │
│   ✅ comp:pyth+ops+sp_fip+home Top 10%     │
│                                             │
│ 預測勝方: 勇士                              │
│ 信心度: ⭐⭐⭐⭐⭐ (高)                      │
│ 建議最低合計賠率: 2.06                      │
└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐
│ (普通場次)                                   │
│ 道奇 @ 遊騎兵                                │
│ ... (簡化版顯示，只列指標數字)                │
│ 預測勝方: 道奇                               │
│ 信心度: ⭐⭐⭐ (中)                           │
└─────────────────────────────────────────────┘
```

**核心 UI 規則：**
1. **`is_top_10_pct: true` 的場次必須有明顯視覺差別**（綠色邊框、⭐ 圖示、置頂）
2. **SP 數據和球隊數據要分區塊顯示**，讓使用者一眼看到「今日先發投手」vs「球隊整季」
3. **複合分數區塊**列出該場主要複合指標的計算結果與預測方
4. **篩選條件勾選清單**顯示這場符合哪些 Top 10% / Top 20% 篩選

---

#### 子頁 2：指標驗證

顯示回測相關性排名表，從 `correlations` 物件渲染。

```
分類顯示 (用 tab 或 section):
  [球隊累積] [先發投手] [複合指標] [全部]

每一列:
  排名 | 圖示 | 指標名 | 命中率 | 進度條 | 樣本數 | z 分數 | 顯著性

排序: 依 pct 降序

顏色規則:
  pct >= 56%  → 深綠 (強信號)
  54-56%      → 淺綠 (有效)
  52-54%      → 灰色 (微弱)
  50-52%      → 淡黃 (持平)
  < 50%       → 紅色 (反向 / 無效)

特別標注:
  - 名稱前綴 sp_xxx → 標「先發投手」標籤
  - composite:xxx → 標「複合」標籤
  - composite 含 +home → 標「主場修正」標籤
  - bp_fatigue → ⚠️ 標示「樣本不足，10年驗證為無效」
  - home_adv → 🏠 標示
```

---

#### 子頁 3：可獲利篩選 (新增)

從 `profitable_filters` 陣列渲染。這是真正能幫使用者賺錢的核心頁面。

```
表格顯示:
┌─────────────────────────────────────────────────────────────────┐
│  排名 | 篩選條件                       | Top% | 樣本 | 命中率 |  
│       |                                |      |      | 串2關  |
│       |                                |      |      | 損益平衡│
├─────────────────────────────────────────────────────────────────┤
│   1   | 🥇 comp:run_diff+sp_fip+home  | 10%  | 175  | 69.7%  │
│       |                                |      |      | 48.6%  │
│       |                                |      |      | 2.06   │
│   2   | 🥈 comp:pyth+ops+sp_fip+home  | 10%  | 175  | 69.1%  │
│       |                                |      |      | 47.8%  │
│       |                                |      |      | 2.09   │
│   3   |    era                         | 10%  | 238  | 67.6%  │
│       |                                |      |      | 45.8%  │
│       |                                |      |      | 2.19   │
└─────────────────────────────────────────────────────────────────┘

頂部說明文字 (重要！):
  💡 如何使用此頁面：
  
  1. 上方列出「歷史命中率最高」的篩選條件
  2. 「Top%」表示該指標差距最大的前 N% 場次
  3. 「損益平衡」表示要正 ROI，串2關合計賠率必須超過此數字
  4. 範例：用 comp:run_diff+sp_fip+home Top 10%，找兩場符合條件的
        比賽串關，只要台彩合計賠率 > 2.06 就有正期望值

底部標示:
  📊 數據樣本：23,950 場 (2015-2025 共 10 季，已排除 2020 COVID 縮短賽季)
  🔄 上次更新：{generated_at}
```

---

### 卡片元件說明

#### 「優劣比較行」格式

```jsx
<div className="stat-row">
  <span className="stat-name">{statName}</span>
  <span className={"stat-val " + (edge === 'away' ? 'win' : '')}>
    {away}
  </span>
  <span className="edge-arrow">{
    edge === 'away' ? '◀' : edge === 'home' ? '▶' : '='
  }</span>
  <span className={"stat-val " + (edge === 'home' ? 'win' : '')}>
    {home}
  </span>
  {edge !== 'even' && <span className="edge-label">✓ {teamName}優</span>}
</div>
```

顏色：
- 優勢方數值用 `--c-win` (綠色)
- 劣勢方數值用 `--c-lose` (淡紅)
- 持平用灰色

#### 「複合分數行」格式

```jsx
<div className="composite-row">
  <span>{compName}</span>
  <span className={value >= 0 ? 'home-favor' : 'away-favor'}>
    {value >= 0 ? '+' : ''}{value.toFixed(2)}
  </span>
  <span>→ {predicts === 'home' ? homeZh : awayZh}</span>
  {bucket === 'Top 10%' && <span>⭐</span>}
</div>
```

#### 「Top 10% 標記」

符合任一 Top 10% 篩選條件的場次卡片：
- 卡片頂部顯示綠色 banner: `⭐ 強烈推薦 - 符合 Top 10% 篩選條件`
- 卡片邊框 2px 綠色
- 在比賽列表頂部置頂

---

### 卡片排序

```js
matchups.sort((a, b) => {
  // 1. Top 10% 場次優先
  if (a.recommendation.is_top_10_pct && !b.recommendation.is_top_10_pct) return -1;
  if (!a.recommendation.is_top_10_pct && b.recommendation.is_top_10_pct) return 1;
  
  // 2. 其餘依優勢差距降序
  const diffA = Math.abs(a.away_edges - a.home_edges);
  const diffB = Math.abs(b.away_edges - b.home_edges);
  return diffB - diffA;
});
```

---

## 資料載入方式

兩個選項（擇一實作）：

### 選項 A：手動貼上 JSON（建議優先做）

```jsx
const [analysisData, setAnalysisData] = useState(null);
const [jsonText, setJsonText] = useState('');

// UI: textarea + 「載入」按鈕
<textarea value={jsonText} onChange={e => setJsonText(e.target.value)} />
<button onClick={() => {
  try {
    setAnalysisData(JSON.parse(jsonText));
  } catch (e) {
    alert('JSON 格式錯誤');
  }
}}>載入分析</button>
```

可考慮存到 localStorage 避免每次重貼：
```js
useEffect(() => {
  const saved = localStorage.getItem('mlb_analysis_json');
  if (saved) setAnalysisData(JSON.parse(saved));
}, []);

// 載入時:
localStorage.setItem('mlb_analysis_json', jsonText);
```

### 選項 B：從 URL 載入

```js
useEffect(() => {
  fetch('mlb_analysis.json')
    .then(r => r.json())
    .then(setAnalysisData);
}, []);
```

---

## 回測發現摘要 (10 年, 23,950 場)

| 類別 | 最強指標 | 命中率 | 結論 |
|------|---------|--------|------|
| **複合 (含 SP+主場)** | comp:pyth+ops+sp_fip+home | **55.8%** | 🥇 全季最強 |
| **複合 (球隊累積)** | comp:ops+whip | 56.0% | 全季次強 |
| **單一球隊指標** | win_pct / run_diff_pg | 55.9% | 強 |
| **單一 SP 指標** | sp_fip | 54.5% | 比球隊 fip (51.9%) 更準 |
| **主場優勢** | home_adv | 53.4% | 微弱但顯著 |
| **牛棚疲勞** | bp_fatigue | 50.0% | ❌ 完全無效 |
| **打擊指標** | ops | 54.6% | 中等 |

### 分層回測 — 真正能正 ROI 的場次

**最強篩選：comp:run_diff+sp_fip+home 的 Top 10% 場次**
- 樣本：175 場
- 單腳命中率：**69.7%**
- 串 2 關命中率：48.6%
- 損益平衡合計賠率：**2.06**

> **這是 v2 升級的最大成果。** 加入今日先發投手與主場修正後，最強篩選從舊版的 67.2% (損益平衡 2.21) 升級到 69.7% (損益平衡 2.06)。台彩典型合計賠率 2.5+ 在這個篩選下有 21% 的正 EV 緩衝。

---

## 相關檔案

- `D:/python/mlb-tracker/mlb_analyzer.py` — 分析引擎
- `D:/python/mlb-tracker/cache/` — API boxscore 快取目錄
- `D:/python/mlb-tracker/cache/games_{season}.json` — 解析後的賽季資料
- `D:/python/mlb-tracker/mlb_analysis.json` — 輸出的分析結果 (web app 讀取)
- `D:/python/mlb-tracker/MLB_INDICATORS.txt` — 30 個指標完整定義與公式

## mlb_analyzer.py 可用指令

```bash
fetch <season>            # 抓單一賽季
range <start> <end>       # 抓多季
rebuild <season>          # 從 cache 重新解析 (不打 API, 加新欄位用)
rebuild range <s> <e>     # 多季重新解析
correlate <season>        # 回測單一賽季
correlate range <s> <e>   # 回測多季合併
today                     # 產出今日分析 JSON
daily                     # 抓當季最新 + 回測 + 今日分析 (每日例行)
```
