"""
MLB 投注分析工具 — 統計相關性回測 + 牛棚追蹤 + 每日分析
用法:
  python mlb_analyzer.py fetch <season>              # 抓單一賽季 (例: fetch 2024)
  python mlb_analyzer.py range <start> <end>         # 抓多季 (例: range 2023 2026)
  python mlb_analyzer.py rebuild <season>            # 從既有 box cache 重新解析 (不打 API)
  python mlb_analyzer.py rebuild range <s> <e>       # 多季重新解析
  python mlb_analyzer.py correlate <season>          # 回測單一賽季
  python mlb_analyzer.py correlate range <s> <e>     # 回測多季合併 (自動存 baseline)
  python mlb_analyzer.py daily [date]                # 每日例行: 抓最新+產生分析 JSON
  python mlb_analyzer.py daily [date] --top5         # 同上, 只顯示 Top 5% 場次
  python mlb_analyzer.py today [date]                # 不抓新資料, 只重新產生分析
  python mlb_analyzer.py today [date] --top5         # 同上, 只顯示 Top 5%

日期參數 (可選):
  daily                → 今天
  daily 4.11           → 2026-04-11
  daily 4/11           → 2026-04-11
  daily 2025-09-28     → 歷史日期
"""
import json, re, sys, os, time
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from math import comb, sqrt
from urllib.request import urlopen, Request
from urllib.error import HTTPError

# ═══════════════════════════════════════════
# 設定
# ═══════════════════════════════════════════
# 各賽季常規賽日期 (regular season only)
SEASON_DATES = {
    2015: ("2015-04-05", "2015-10-04"),
    2016: ("2016-04-03", "2016-10-02"),
    2017: ("2017-04-02", "2017-10-01"),
    2018: ("2018-03-29", "2018-10-01"),
    2019: ("2019-03-20", "2019-09-29"),
    2020: ("2020-07-23", "2020-09-27"),  # COVID 縮短，僅 60 場/隊
    2021: ("2021-04-01", "2021-10-03"),
    2022: ("2022-04-07", "2022-10-05"),
    2023: ("2023-03-30", "2023-10-01"),
    2024: ("2024-03-28", "2024-09-29"),
    2025: ("2025-03-27", "2025-09-28"),
    2026: ("2026-03-25", "2026-09-27"),  # 估計
}
EXCLUDED_SEASONS = {2020}  # COVID 縮短賽季，預設跳過
CURRENT_SEASON = 2026

MIN_GAMES = 5          # 球隊至少打幾場才納入分析
MIN_STARTS = 2         # 投手至少先發幾場才納入分析 (從 3 降到 2 以覆蓋開季)
BULLPEN_WINDOW = 3     # 牛棚疲勞追蹤天數
FIP_CONSTANT = 3.10    # 近似 cFIP
HOME_ADV_BONUS = 0.15  # 主場優勢加成 (z-score 單位，約對應 1-2% 勝率)
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
BASELINE_FILE = "baseline_10y.json"
ANALYSIS_OUTPUT = "mlb_analysis.json"
API_BASE = "https://statsapi.mlb.com/api/v1"

# ═══════════════════════════════════════════
# 盤口定義 — 支援多種 bet type 並行回測
# ═══════════════════════════════════════════
# 每種盤口有獨立的結果分析、bucket threshold、profitable filters
# type: 'directional' (ML, 讓分) 用「差距」; 'total' (大小分) 用「加總」
BET_TYPES = [
    # (key, name, type, line, outcome_fn)
    ('ml',         '不讓分',      'directional', 0,   lambda g: g.winner_side),
    ('spread_1.5', '讓分 1.5',   'directional', 1.5,
        lambda g: 'home' if (g.home_score - g.away_score) >= 2 else 'away'),
    ('spread_2.5', '讓分 2.5',   'directional', 2.5,
        lambda g: 'home' if (g.home_score - g.away_score) >= 3 else 'away'),
    ('total_7.5',  '大小分 7.5', 'total',       7.5,
        lambda g: 'over' if (g.home_score + g.away_score) > 7.5 else 'under'),
    ('total_8.5',  '大小分 8.5', 'total',       8.5,
        lambda g: 'over' if (g.home_score + g.away_score) > 8.5 else 'under'),
    ('total_9.5',  '大小分 9.5', 'total',       9.5,
        lambda g: 'over' if (g.home_score + g.away_score) > 9.5 else 'under'),
]

# 複合指標定義 (directional - ML 與 讓分用)
# 格式: (name, [(stat_key, weight, source)], use_home_adv)
# source: "team" 用球隊累積, "sp" 用今日先發投手
COMPOSITES_DEF = [
    # === 純球隊複合 ===
    ("ops+era", [("ops", 1, "team"), ("era", -1, "team")], False),
    ("ops+fip", [("ops", 1, "team"), ("fip", -1, "team")], False),
    ("ops+whip", [("ops", 1, "team"), ("whip", -1, "team")], False),
    ("run_diff+fip", [("run_diff_per_game", 1, "team"), ("fip", -1, "team")], False),
    ("pyth+ops+fip", [("pyth_pct", 1, "team"), ("ops", 1, "team"), ("fip", -1, "team")], False),
    ("slg+era", [("slg", 1, "team"), ("era", -1, "team")], False),
    ("obp+whip", [("obp", 1, "team"), ("whip", -1, "team")], False),
    # === 加入主場優勢 ===
    ("ops+era+home", [("ops", 1, "team"), ("era", -1, "team")], True),
    ("run_diff+fip+home", [("run_diff_per_game", 1, "team"), ("fip", -1, "team")], True),
    ("pyth+ops+fip+home", [("pyth_pct", 1, "team"), ("ops", 1, "team"), ("fip", -1, "team")], True),
    # === 用今日先發投手 ===
    ("ops+sp_era", [("ops", 1, "team"), ("sp_era", -1, "sp")], False),
    ("ops+sp_fip", [("ops", 1, "team"), ("sp_fip", -1, "sp")], False),
    ("ops+sp_whip", [("ops", 1, "team"), ("sp_whip", -1, "sp")], False),
    ("run_diff+sp_fip", [("run_diff_per_game", 1, "team"), ("sp_fip", -1, "sp")], False),
    ("pyth+ops+sp_fip", [("pyth_pct", 1, "team"), ("ops", 1, "team"), ("sp_fip", -1, "sp")], False),
    # === SP + 主場優勢 (完整組合) ===
    ("ops+sp_era+home", [("ops", 1, "team"), ("sp_era", -1, "sp")], True),
    ("ops+sp_fip+home", [("ops", 1, "team"), ("sp_fip", -1, "sp")], True),
    ("pyth+ops+sp_fip+home", [("pyth_pct", 1, "team"), ("ops", 1, "team"), ("sp_fip", -1, "sp")], True),
    ("run_diff+sp_fip+home", [("run_diff_per_game", 1, "team"), ("sp_fip", -1, "sp")], True),
]

# 總和型複合指標 (total - 大小分用)
# 加總兩隊的值，正權重 = 高值 → 偏大分, 負權重 = 高值 → 偏小分
# 例：ops 加總越高 → 越可能大分 (weight=+1)
#     k9 加總越高 → 越可能小分 (weight=-1)
TOTAL_COMPOSITES_DEF = [
    # === 純打擊 ===
    ("runs_pg",            [("runs_per_game", 1, "team")]),
    ("ops",                [("ops", 1, "team")]),
    ("slg",                [("slg", 1, "team")]),
    ("hr_pg",              [("hr_per_game", 1, "team")]),
    # === 純投球 (團隊) ===
    ("era",                [("era", 1, "team")]),
    ("fip",                [("fip", 1, "team")]),
    ("whip",               [("whip", 1, "team")]),
    ("k9_inv",             [("k9", -1, "team")]),
    # === 純投球 (今日先發) — 避免被 team OPS 抵消 ===
    ("sp_era_only",        [("sp_era", 1, "sp")]),
    ("sp_fip_only",        [("sp_fip", 1, "sp")]),
    ("sp_whip_only",       [("sp_whip", 1, "sp")]),
    ("sp_hr9_only",        [("sp_hr9", 1, "sp")]),
    ("sp_k9_inv",          [("sp_k9", -1, "sp")]),
    # === 打擊+投球組合 (團隊) ===
    ("runs+era",           [("runs_per_game", 1, "team"), ("era", 1, "team")]),
    ("ops+era",            [("ops", 1, "team"), ("era", 1, "team")]),
    ("ops+fip",            [("ops", 1, "team"), ("fip", 1, "team")]),
    ("ops+whip",           [("ops", 1, "team"), ("whip", 1, "team")]),
    ("runs+fip",           [("runs_per_game", 1, "team"), ("fip", 1, "team")]),
    ("runs-k9",            [("runs_per_game", 1, "team"), ("k9", -1, "team")]),
    ("ops-k9",             [("ops", 1, "team"), ("k9", -1, "team")]),
    # === 打擊+先發投手組合 ===
    ("ops+sp_era",         [("ops", 1, "team"), ("sp_era", 1, "sp")]),
    ("ops+sp_fip",         [("ops", 1, "team"), ("sp_fip", 1, "sp")]),
    ("runs+sp_era",        [("runs_per_game", 1, "team"), ("sp_era", 1, "sp")]),
    ("runs+sp_fip",        [("runs_per_game", 1, "team"), ("sp_fip", 1, "sp")]),
    ("ops-sp_k9",          [("ops", 1, "team"), ("sp_k9", -1, "sp")]),
    ("ops+sp_whip",        [("ops", 1, "team"), ("sp_whip", 1, "sp")]),
    # === 先發投手加權 (2x) — 讓極端 SP 值的信號更強 ===
    ("ops+2sp_fip",        [("ops", 1, "team"), ("sp_fip", 2, "sp")]),
    ("runs+2sp_era",       [("runs_per_game", 1, "team"), ("sp_era", 2, "sp")]),
    ("2sp_fip+sp_era",     [("sp_fip", 2, "sp"), ("sp_era", 1, "sp")]),
]

TEAM_ZH = {
    "Arizona Diamondbacks": "響尾蛇", "Atlanta Braves": "勇士",
    "Baltimore Orioles": "金鶯", "Boston Red Sox": "紅襪",
    "Chicago Cubs": "小熊", "Chicago White Sox": "白襪",
    "Cincinnati Reds": "紅人", "Cleveland Guardians": "守護者",
    "Colorado Rockies": "洛磯", "Detroit Tigers": "老虎",
    "Houston Astros": "太空人", "Kansas City Royals": "皇家",
    "Los Angeles Angels": "天使", "Los Angeles Dodgers": "道奇",
    "Miami Marlins": "馬林魚", "Milwaukee Brewers": "釀酒人",
    "Minnesota Twins": "雙城", "New York Mets": "大都會",
    "New York Yankees": "洋基", "Oakland Athletics": "運動家",
    "Athletics": "運動家",
    "Philadelphia Phillies": "費城人", "Pittsburgh Pirates": "海盜",
    "San Diego Padres": "教士", "San Francisco Giants": "巨人",
    "Seattle Mariners": "水手", "St. Louis Cardinals": "紅雀",
    "Tampa Bay Rays": "光芒", "Texas Rangers": "遊騎兵",
    "Toronto Blue Jays": "藍鳥", "Washington Nationals": "國民",
}


# ═══════════════════════════════════════════
# 進度條
# ═══════════════════════════════════════════
def progress_bar(label, current, total, start_time, bar_width=30):
    """印出進度條 (覆寫同一行)"""
    if total == 0:
        return
    pct = current / total
    filled = int(bar_width * pct)
    bar = "█" * filled + "░" * (bar_width - filled)

    elapsed = time.time() - start_time
    if current > 0:
        eta = elapsed / current * (total - current)
        eta_str = f"{int(eta // 60)}m{int(eta % 60):02d}s"
    else:
        eta_str = "--m--s"
    elapsed_str = f"{int(elapsed // 60)}m{int(elapsed % 60):02d}s"

    line = f"  {label} [{bar}] {pct*100:5.1f}% | {current}/{total} | 已過 {elapsed_str} | 剩約 {eta_str}"
    sys.stdout.write("\r" + line + " " * 5)
    sys.stdout.flush()


def progress_done(label):
    """完成進度條後換行"""
    sys.stdout.write("\n")
    sys.stdout.flush()


def parse_date_arg(arg):
    """解析日期參數，支援多種格式:
    4.11, 4/11, 4-11       → 當年 4 月 11 日
    2026-04-11, 2026.04.11 → 完整日期
    """
    if not arg:
        return None
    s = arg.strip().replace('.', '-').replace('/', '-')
    parts = s.split('-')

    if len(parts) == 2:  # 月-日 (補當年)
        month, day = int(parts[0]), int(parts[1])
        return f"{CURRENT_SEASON}-{month:02d}-{day:02d}"
    elif len(parts) == 3:  # 年-月-日
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        return f"{year:04d}-{month:02d}-{day:02d}"
    else:
        raise ValueError(f"無法解析日期: {arg}")


# ═══════════════════════════════════════════
# API 工具
# ═══════════════════════════════════════════
def api_get(path, max_retries=4):
    """API GET with exponential backoff retry"""
    url = f"{API_BASE}{path}"
    for attempt in range(max_retries):
        try:
            req = Request(url, headers={"User-Agent": "MLB-Analyzer/1.0"})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 404:
                return None  # 不重試
            if attempt == max_retries - 1:
                sys.stdout.write(f"\n  API HTTPError {e.code}: {path}\n")
                return None
            time.sleep(2 ** attempt)  # 1s, 2s, 4s, 8s
        except Exception as e:
            if attempt == max_retries - 1:
                sys.stdout.write(f"\n  API failed after {max_retries} retries: {path} ({type(e).__name__})\n")
                return None
            time.sleep(2 ** attempt)
    return None


def parse_ip(ip_str):
    """將 '6.1' 轉成 outs 數 (19)。MLB 用 .1=1/3, .2=2/3"""
    if isinstance(ip_str, (int, float)):
        whole = int(ip_str)
        frac = round((ip_str - whole) * 10)
        return whole * 3 + frac
    s = str(ip_str)
    if '.' in s:
        parts = s.split('.')
        return int(parts[0]) * 3 + int(parts[1])
    return int(s) * 3


def ip_from_outs(outs):
    """outs 轉回 IP 顯示格式"""
    return f"{outs // 3}.{outs % 3}"


# ═══════════════════════════════════════════
# 資料結構
# ═══════════════════════════════════════════
@dataclass
class PitcherLine:
    name: str = ""
    outs: int = 0        # 用 outs 追蹤，避免小數問題
    h: int = 0
    r: int = 0
    er: int = 0
    bb: int = 0
    so: int = 0
    hr: int = 0
    pitches: int = 0
    is_starter: bool = True


@dataclass
class TeamGameStats:
    """單場比賽的球隊統計"""
    # 打擊
    ab: int = 0
    h: int = 0
    doubles: int = 0
    triples: int = 0
    hr: int = 0
    rbi: int = 0
    bb: int = 0
    so: int = 0
    hbp: int = 0
    sf: int = 0
    runs: int = 0
    lob: int = 0
    # 投球 (全隊)
    p_outs: int = 0
    p_h: int = 0
    p_r: int = 0
    p_er: int = 0
    p_bb: int = 0
    p_so: int = 0
    p_hr: int = 0
    # 牛棚
    relief_outs: int = 0
    relief_pitches: int = 0
    relief_count: int = 0


@dataclass
class StarterStats:
    """單場先發投手數據"""
    pitcher_id: int = 0
    name: str = ""
    outs: int = 0
    h: int = 0
    r: int = 0       # total runs (含非自責分)
    er: int = 0
    bb: int = 0
    so: int = 0
    hr: int = 0
    pitches: int = 0


@dataclass
class GameRecord:
    date: str = ""
    game_pk: int = 0
    away_name: str = ""
    home_name: str = ""
    away_score: int = 0
    home_score: int = 0
    winner_side: str = ""   # "away" or "home"
    away_stats: TeamGameStats = field(default_factory=TeamGameStats)
    home_stats: TeamGameStats = field(default_factory=TeamGameStats)
    away_starter: StarterStats = field(default_factory=StarterStats)
    home_starter: StarterStats = field(default_factory=StarterStats)


@dataclass
class PitcherCumulative:
    """先發投手累積數據（單季）"""
    pitcher_id: int = 0
    name: str = ""
    starts: int = 0
    outs: int = 0
    h: int = 0
    er: int = 0
    bb: int = 0
    so: int = 0
    hr: int = 0
    pitches: int = 0
    start_log: list = field(default_factory=list)  # 逐場明細

    def add_start(self, s: StarterStats, date='', vs='', vs_zh='',
                  team_score=0, opp_score=0, team_won=False):
        self.starts += 1
        self.outs += s.outs
        self.h += s.h
        self.er += s.er
        self.bb += s.bb
        self.so += s.so
        self.hr += s.hr
        self.pitches += s.pitches

        # 存逐場明細 (用於 recent_starts)
        if date:
            self.start_log.append({
                'date': date,
                'vs': vs,
                'vs_zh': vs_zh,
                'ip': ip_from_outs(s.outs),
                'h': s.h,
                'r': s.r,
                'er': s.er,
                'bb': s.bb,
                'so': s.so,
                'hr': s.hr,
                'pitches': s.pitches,
                'result': 'W' if team_won else 'L',
                'team_score': f"{team_score}-{opp_score}",
            })

    def recent_starts(self, n=3):
        """回傳最近 n 場先發明細 (最近的在前)"""
        return list(reversed(self.start_log[-n:])) if self.start_log else []

    def snapshot(self):
        ip = self.outs / 3 if self.outs > 0 else 1
        return {
            'sp_starts': self.starts,
            'sp_era': (self.er / ip) * 9,
            'sp_whip': (self.bb + self.h) / ip,
            'sp_fip': ((13 * self.hr + 3 * self.bb - 2 * self.so) / ip) + FIP_CONSTANT,
            'sp_k9': (self.so / ip) * 9,
            'sp_bb9': (self.bb / ip) * 9,
            'sp_hr9': (self.hr / ip) * 9,
            'sp_k_bb': self.so / self.bb if self.bb > 0 else self.so,
            'sp_pitches_per_start': self.pitches / self.starts if self.starts > 0 else 0,
        }


@dataclass
class TeamCumulative:
    """球隊累積統計 — 所有值為加總"""
    games: int = 0
    wins: int = 0
    losses: int = 0
    # 打擊
    ab: int = 0
    h: int = 0
    doubles: int = 0
    triples: int = 0
    hr: int = 0
    bb: int = 0
    so: int = 0
    hbp: int = 0
    sf: int = 0
    runs_scored: int = 0
    # 投球
    p_outs: int = 0
    p_h: int = 0
    p_er: int = 0
    p_bb: int = 0
    p_so: int = 0
    p_hr: int = 0
    runs_allowed: int = 0
    # 牛棚歷史 (近N天用 bullpen_log)
    bullpen_log: list = field(default_factory=list)  # [(date, outs, pitches, count)]

    def add_game(self, date, stats: TeamGameStats, won: bool):
        self.games += 1
        if won:
            self.wins += 1
        else:
            self.losses += 1
        self.ab += stats.ab
        self.h += stats.h
        self.doubles += stats.doubles
        self.triples += stats.triples
        self.hr += stats.hr
        self.bb += stats.bb
        self.so += stats.so
        self.hbp += stats.hbp
        self.sf += stats.sf
        self.runs_scored += stats.runs
        self.p_outs += stats.p_outs
        self.p_h += stats.p_h
        self.p_er += stats.p_er
        self.p_bb += stats.p_bb
        self.p_so += stats.p_so
        self.p_hr += stats.p_hr
        self.runs_allowed += stats.p_r
        self.bullpen_log.append((date, stats.relief_outs, stats.relief_pitches, stats.relief_count))

    def snapshot(self, as_of_date=None):
        """產出快照 — 計算所有率值"""
        s = {}
        s['games'] = self.games
        s['wins'] = self.wins
        s['losses'] = self.losses
        s['win_pct'] = self.wins / self.games if self.games > 0 else 0.5

        # 打擊
        s['avg'] = self.h / self.ab if self.ab > 0 else 0
        reach = self.h + self.bb + self.hbp
        pa = self.ab + self.bb + self.hbp + self.sf
        s['obp'] = reach / pa if pa > 0 else 0
        tb = self.h + self.doubles + 2 * self.triples + 3 * self.hr
        s['slg'] = tb / self.ab if self.ab > 0 else 0
        s['ops'] = s['obp'] + s['slg']
        s['runs_per_game'] = self.runs_scored / self.games if self.games > 0 else 0
        s['hr_per_game'] = self.hr / self.games if self.games > 0 else 0
        s['bb_rate'] = self.bb / pa if pa > 0 else 0
        s['so_rate'] = self.so / pa if pa > 0 else 0

        # 投球
        ip = self.p_outs / 3 if self.p_outs > 0 else 1
        s['era'] = (self.p_er / ip) * 9
        s['whip'] = (self.p_bb + self.p_h) / ip
        s['fip'] = ((13 * self.p_hr + 3 * self.p_bb - 2 * self.p_so) / ip) + FIP_CONSTANT
        s['k9'] = (self.p_so / ip) * 9
        s['bb9'] = (self.p_bb / ip) * 9
        s['hr9'] = (self.p_hr / ip) * 9
        s['k_bb_ratio'] = self.p_so / self.p_bb if self.p_bb > 0 else self.p_so
        s['ra_per_game'] = self.runs_allowed / self.games if self.games > 0 else 0

        # 得失分差
        s['run_diff'] = self.runs_scored - self.runs_allowed
        s['run_diff_per_game'] = s['run_diff'] / self.games if self.games > 0 else 0

        # 畢氏期望勝率
        rs2 = self.runs_scored ** 2
        ra2 = self.runs_allowed ** 2
        s['pyth_pct'] = rs2 / (rs2 + ra2) if (rs2 + ra2) > 0 else 0.5

        # 牛棚疲勞
        if as_of_date:
            cutoff = datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=BULLPEN_WINDOW)
            recent = [(d, o, p, c) for d, o, p, c in self.bullpen_log
                      if datetime.strptime(d, "%Y-%m-%d") > cutoff]
            s['bp_outs_3d'] = sum(o for _, o, _, _ in recent)
            s['bp_pitches_3d'] = sum(p for _, p, _, _ in recent)
            s['bp_count_3d'] = sum(c for _, _, _, c in recent)
            # 疲勞分數 (0-1)，基準：3天平均約 9 outs / 135 pitches
            fatigue_ip = s['bp_outs_3d'] / 9.0   # 相對於 3 局/天的基準
            fatigue_pitch = s['bp_pitches_3d'] / 135.0
            s['bp_fatigue'] = min(1.0, (fatigue_ip * 0.4 + fatigue_pitch * 0.6))
        else:
            s['bp_outs_3d'] = 0
            s['bp_pitches_3d'] = 0
            s['bp_count_3d'] = 0
            s['bp_fatigue'] = 0

        return s


# ═══════════════════════════════════════════
# 第一步：從 MLB API 抓取賽季資料並快取
# ═══════════════════════════════════════════
def fetch_season_games(season):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"games_{season}.json")

    if season not in SEASON_DATES:
        print(f"未支援的賽季: {season}")
        return []

    start_str, end_str = SEASON_DATES[season]
    today = datetime.now().strftime("%Y-%m-%d")
    # 當季尚未結束就用今天為終點
    if end_str > today:
        end_str = today

    print(f"\n=== 抓取 {season} 賽季 ({start_str} ~ {end_str}) ===")

    # 載入既有快取避免重複處理
    existing_games = {}
    if os.path.exists(cache_file):
        with open(cache_file, 'r', encoding='utf-8') as f:
            for d in json.load(f):
                existing_games[d['game_pk']] = d
        print(f"  已快取 game 資料: {len(existing_games)} 場")

    # === Phase 1: 先掃 schedule 取得所有 gamePk ===
    print(f"  Phase 1/2: 掃描賽程...")
    current = datetime.strptime(start_str, "%Y-%m-%d")
    end = datetime.strptime(end_str, "%Y-%m-%d")
    total_days = (end - current).days + 1

    all_game_pks = []  # [(date_str, gpk), ...]
    day_idx = 0
    phase1_start = time.time()

    while current <= end:
        day_idx += 1
        date_str = current.strftime("%Y-%m-%d")

        sched = api_get(f"/schedule?date={date_str}&sportId=1&gameType=R")
        if sched and sched.get('dates'):
            for game_data in sched['dates'][0].get('games', []):
                status = game_data.get('status', {}).get('abstractGameState', '')
                if status != 'Final':
                    continue
                if game_data.get('gameType') != 'R':
                    continue
                all_game_pks.append((date_str, game_data['gamePk']))

        progress_bar(f"{season} 賽程掃描", day_idx, total_days, phase1_start)
        current += timedelta(days=1)
    progress_done("")

    print(f"  共找到 {len(all_game_pks)} 場已完成的常規賽")

    # === Phase 2: 抓取 boxscore (跳過已快取) ===
    to_fetch = [(d, p) for d, p in all_game_pks if p not in existing_games]
    print(f"  Phase 2/2: 抓取 boxscore ({len(to_fetch)} 場新資料, {len(existing_games)} 場已快取)")

    all_games = []
    new_count = 0
    phase2_start = time.time()

    # 先把已快取的資料加入結果
    cached_pks = set()
    for date_str, gpk in all_game_pks:
        if gpk in existing_games:
            all_games.append(existing_games[gpk])
            cached_pks.add(gpk)

    # 抓新資料
    save_interval = 200  # 每抓 200 場存檔一次

    def save_partial():
        """中途存檔，避免 crash 損失進度"""
        sorted_games = sorted(all_games, key=lambda x: (x['date'], x['game_pk']))
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(sorted_games, f, ensure_ascii=False, indent=2)

    try:
        for idx, (date_str, gpk) in enumerate(to_fetch, 1):
            box_cache = os.path.join(CACHE_DIR, f"box_{gpk}.json")
            if os.path.exists(box_cache):
                with open(box_cache, 'r') as f:
                    box = json.load(f)
            else:
                box = api_get(f"/game/{gpk}/boxscore")
                if box:
                    with open(box_cache, 'w') as f:
                        json.dump(box, f)
                    time.sleep(0.1)
                else:
                    progress_bar(f"{season} boxscore", idx, len(to_fetch), phase2_start)
                    continue

            game = parse_boxscore(box, gpk, date_str)
            if game:
                d = game_to_dict(game)
                all_games.append(d)
                new_count += 1

            # 每 N 場存檔
            if new_count > 0 and new_count % save_interval == 0:
                save_partial()

            progress_bar(f"{season} boxscore", idx, len(to_fetch), phase2_start)

        if to_fetch:
            progress_done("")
    except KeyboardInterrupt:
        sys.stdout.write("\n  已中斷，正在存檔...\n")
        save_partial()
        sys.stdout.write(f"  已存 {len(all_games)} 場到 {cache_file}\n")
        raise

    # 最終排序儲存
    all_games.sort(key=lambda x: (x['date'], x['game_pk']))
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(all_games, f, ensure_ascii=False, indent=2)

    print(f"  完成！{season} 賽季共 {len(all_games)} 場 (新增 {new_count})")
    return all_games


def game_to_dict(game):
    """GameRecord -> dict (用於 JSON 序列化)"""
    return {
        'date': game.date, 'game_pk': game.game_pk,
        'away_name': game.away_name, 'home_name': game.home_name,
        'away_score': game.away_score, 'home_score': game.home_score,
        'winner_side': game.winner_side,
        'away_stats': asdict(game.away_stats),
        'home_stats': asdict(game.home_stats),
        'away_starter': asdict(game.away_starter),
        'home_starter': asdict(game.home_starter),
    }


def rebuild_season(season):
    """從既有 box cache 重新解析該季 games_*.json (不打 API)"""
    if season not in SEASON_DATES:
        print(f"未支援的賽季: {season}")
        return

    print(f"\n=== 重新解析 {season} 賽季 ===")

    start_str, end_str = SEASON_DATES[season]
    today = datetime.now().strftime("%Y-%m-%d")
    if end_str > today:
        end_str = today

    # 從 schedule 取所有 gamePk
    print(f"  Phase 1/2: 掃描賽程...")
    current = datetime.strptime(start_str, "%Y-%m-%d")
    end = datetime.strptime(end_str, "%Y-%m-%d")
    total_days = (end - current).days + 1

    all_pks = []  # [(date, gpk), ...]
    day_idx = 0
    p1_start = time.time()

    while current <= end:
        day_idx += 1
        date_str = current.strftime("%Y-%m-%d")
        sched = api_get(f"/schedule?date={date_str}&sportId=1&gameType=R")
        if sched and sched.get('dates'):
            for game_data in sched['dates'][0].get('games', []):
                if game_data.get('status', {}).get('abstractGameState') != 'Final':
                    continue
                if game_data.get('gameType') != 'R':
                    continue
                all_pks.append((date_str, game_data['gamePk']))
        progress_bar(f"{season} 掃描", day_idx, total_days, p1_start)
        current += timedelta(days=1)
    progress_done("")

    # Phase 2: 解析快取
    print(f"  Phase 2/2: 解析 {len(all_pks)} 場 box cache...")
    p2_start = time.time()
    parsed = []
    missing = 0

    for idx, (date_str, gpk) in enumerate(all_pks, 1):
        box_cache = os.path.join(CACHE_DIR, f"box_{gpk}.json")
        if not os.path.exists(box_cache):
            missing += 1
            continue
        with open(box_cache, 'r') as f:
            box = json.load(f)
        game = parse_boxscore(box, gpk, date_str)
        if game:
            parsed.append(game_to_dict(game))
        progress_bar(f"{season} 解析", idx, len(all_pks), p2_start)
    progress_done("")

    # 去重 + 儲存
    seen_pks = set()
    deduped = []
    for p in parsed:
        if p['game_pk'] not in seen_pks:
            seen_pks.add(p['game_pk'])
            deduped.append(p)
    deduped.sort(key=lambda x: (x['date'], x['game_pk']))
    cache_file = os.path.join(CACHE_DIR, f"games_{season}.json")
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(deduped, f, ensure_ascii=False, indent=2)

    dupes = len(parsed) - len(deduped)
    print(f"  完成！{season} 共 {len(deduped)} 場 (缺 {missing} 場, 去重 {dupes} 場)")
    return parsed


def fetch_season_range(start_year, end_year):
    """抓取多季資料 (自動跳過 EXCLUDED_SEASONS)"""
    seasons_to_fetch = [y for y in range(start_year, end_year + 1) if y not in EXCLUDED_SEASONS]
    skipped = [y for y in range(start_year, end_year + 1) if y in EXCLUDED_SEASONS]

    print(f"\n=== 抓取 {start_year} ~ {end_year} ({len(seasons_to_fetch)} 季) ===")
    if skipped:
        print(f"  跳過: {skipped} (COVID 或其他特殊賽季)")

    total = 0
    for year in seasons_to_fetch:
        games = fetch_season_games(year)
        total += len(games)
    print(f"\n全部完成！共 {total} 場比賽")


def parse_boxscore(box, game_pk, date_str):
    """解析 boxscore API 回傳的資料"""
    try:
        away_team = box['teams']['away']['team']
        home_team = box['teams']['home']['team']

        away_info = box['teams']['away'].get('teamStats', {})
        home_info = box['teams']['home'].get('teamStats', {})

        if not away_info or not home_info:
            return None

        away_batting = away_info.get('batting', {})
        home_batting = home_info.get('batting', {})
        away_pitching = away_info.get('pitching', {})
        home_pitching = home_info.get('pitching', {})

        away_score = int(away_batting.get('runs', 0))
        home_score = int(home_batting.get('runs', 0))

        if away_score == home_score:
            return None  # 不該發生

        def make_stats(batting, pitching, box_team):
            s = TeamGameStats()
            s.ab = int(batting.get('atBats', 0))
            s.h = int(batting.get('hits', 0))
            s.doubles = int(batting.get('doubles', 0))
            s.triples = int(batting.get('triples', 0))
            s.hr = int(batting.get('homeRuns', 0))
            s.rbi = int(batting.get('rbi', 0))
            s.bb = int(batting.get('baseOnBalls', 0))
            s.so = int(batting.get('strikeOuts', 0))
            s.hbp = int(batting.get('hitByPitch', 0))
            s.sf = int(batting.get('sacFlies', 0))
            s.runs = int(batting.get('runs', 0))
            s.lob = int(batting.get('leftOnBase', 0))

            s.p_outs = parse_ip(pitching.get('inningsPitched', '0'))
            s.p_h = int(pitching.get('hits', 0))
            s.p_r = int(pitching.get('runs', 0))
            s.p_er = int(pitching.get('earnedRuns', 0))
            s.p_bb = int(pitching.get('baseOnBalls', 0))
            s.p_so = int(pitching.get('strikeOuts', 0))
            s.p_hr = int(pitching.get('homeRuns', 0))

            # 牛棚統計
            pitcher_ids = box_team.get('pitchers', [])
            if len(pitcher_ids) > 1:
                for pid in pitcher_ids[1:]:  # 跳過先發
                    p_key = f"ID{pid}"
                    p_data = box_team.get('players', {}).get(p_key, {})
                    p_stats = p_data.get('stats', {}).get('pitching', {})
                    if p_stats:
                        s.relief_outs += parse_ip(p_stats.get('inningsPitched', '0'))
                        pc = p_stats.get('numberOfPitches', p_stats.get('pitchesThrown', 0))
                        s.relief_pitches += int(pc) if pc else 0
                        s.relief_count += 1

            return s

        def extract_starter(box_team):
            """從球隊 boxscore 抽出先發投手 (pitchers[0])"""
            starter = StarterStats()
            pitcher_ids = box_team.get('pitchers', [])
            if not pitcher_ids:
                return starter
            sp_id = pitcher_ids[0]
            p_key = f"ID{sp_id}"
            p_data = box_team.get('players', {}).get(p_key, {})
            p_stats = p_data.get('stats', {}).get('pitching', {})
            if not p_stats:
                return starter
            starter.pitcher_id = sp_id
            starter.name = p_data.get('person', {}).get('fullName', '')
            starter.outs = parse_ip(p_stats.get('inningsPitched', '0'))
            starter.h = int(p_stats.get('hits', 0))
            starter.r = int(p_stats.get('runs', 0))
            starter.er = int(p_stats.get('earnedRuns', 0))
            starter.bb = int(p_stats.get('baseOnBalls', 0))
            starter.so = int(p_stats.get('strikeOuts', 0))
            starter.hr = int(p_stats.get('homeRuns', 0))
            pc = p_stats.get('numberOfPitches', p_stats.get('pitchesThrown', 0))
            starter.pitches = int(pc) if pc else 0
            return starter

        game = GameRecord(
            date=date_str,
            game_pk=game_pk,
            away_name=away_team.get('name', ''),
            home_name=home_team.get('name', ''),
            away_score=away_score,
            home_score=home_score,
            winner_side="away" if away_score > home_score else "home",
            away_stats=make_stats(away_batting, away_pitching, box['teams']['away']),
            home_stats=make_stats(home_batting, home_pitching, box['teams']['home']),
            away_starter=extract_starter(box['teams']['away']),
            home_starter=extract_starter(box['teams']['home']),
        )
        return game
    except Exception as e:
        print(f"\n  解析 {game_pk} 失敗: {e}")
        return None


def load_cached_games(seasons):
    """載入一個或多個賽季的快取資料

    seasons: int 或 list[int]
    """
    if isinstance(seasons, int):
        seasons = [seasons]

    games = []
    for season in seasons:
        cache_file = os.path.join(CACHE_DIR, f"games_{season}.json")
        if not os.path.exists(cache_file):
            print(f"找不到 {season} 賽季快取，請先執行 fetch {season}")
            continue

        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        seen_pks = set()
        for d in data:
            gpk = d['game_pk']
            if gpk in seen_pks:
                continue  # 跳過重複的 gamePk
            seen_pks.add(gpk)
            g = GameRecord(
                date=d['date'], game_pk=d['game_pk'],
                away_name=d['away_name'], home_name=d['home_name'],
                away_score=d['away_score'], home_score=d['home_score'],
                winner_side=d['winner_side'],
                away_stats=TeamGameStats(**d['away_stats']),
                home_stats=TeamGameStats(**d['home_stats']),
                away_starter=StarterStats(**{k: v for k, v in d['away_starter'].items() if k in StarterStats.__dataclass_fields__}) if 'away_starter' in d else StarterStats(),
                home_starter=StarterStats(**{k: v for k, v in d['home_starter'].items() if k in StarterStats.__dataclass_fields__}) if 'home_starter' in d else StarterStats(),
            )
            games.append(g)

        print(f"  載入 {season}: {len(data)} 場")

    games.sort(key=lambda x: (x.date, x.game_pk))
    return games


# ═══════════════════════════════════════════
# 第二步：相關性回測
# ═══════════════════════════════════════════
def run_correlation_analysis(games):
    print(f"\n=== 相關性回測 ({len(games)} 場) ===\n")

    # 按日期排序
    games.sort(key=lambda x: (x.date, x.game_pk))

    # 累積追蹤器 - 每季獨立 (避免跨季污染)
    trackers = {}            # 球隊累積
    pitcher_trackers = {}    # 投手累積 (per starter)
    current_season = None

    # 收集 matchup 數據
    matchups = []
    has_starter_data = any(g.away_starter.pitcher_id > 0 for g in games[:20])

    for g in games:
        game_season = int(g.date[:4])

        # 跨季時重置 trackers
        if game_season != current_season:
            if current_season is not None:
                print(f"  → 進入 {game_season} 賽季 (重置累積追蹤)")
            trackers = {}
            pitcher_trackers = {}
            current_season = game_season

        for name in [g.away_name, g.home_name]:
            if name not in trackers:
                trackers[name] = TeamCumulative()

        # 取得賽前球隊快照
        away_snap = trackers[g.away_name].snapshot(g.date)
        home_snap = trackers[g.home_name].snapshot(g.date)

        # 取得賽前先發投手快照 (今日的先發投手)
        away_sp_snap = None
        home_sp_snap = None
        if g.away_starter.pitcher_id > 0:
            sp_id = g.away_starter.pitcher_id
            if sp_id in pitcher_trackers:
                away_sp_snap = pitcher_trackers[sp_id].snapshot()
        if g.home_starter.pitcher_id > 0:
            sp_id = g.home_starter.pitcher_id
            if sp_id in pitcher_trackers:
                home_sp_snap = pitcher_trackers[sp_id].snapshot()

        # 只有雙方球隊都打了足夠場次才納入
        if away_snap['games'] >= MIN_GAMES and home_snap['games'] >= MIN_GAMES:
            m = {
                'date': g.date,
                'season': game_season,
                'away': g.away_name,
                'home': g.home_name,
                'away_snap': away_snap,
                'home_snap': home_snap,
                'away_sp_snap': away_sp_snap,
                'home_sp_snap': home_sp_snap,
                'winner_side': g.winner_side,
                'total_runs': g.away_score + g.home_score,
                'score_diff': g.home_score - g.away_score,  # home 視角
            }
            # 預先計算每個 bet type 的 outcome
            for bt_key, _, _, _, outcome_fn in BET_TYPES:
                m[f'out_{bt_key}'] = outcome_fn(g)
            matchups.append(m)

        # 更新球隊累積（在記錄 matchup 之後！）
        trackers[g.away_name].add_game(g.date, g.away_stats, g.winner_side == "away")
        trackers[g.home_name].add_game(g.date, g.home_stats, g.winner_side == "home")

        # 更新投手累積
        if g.away_starter.pitcher_id > 0:
            pid = g.away_starter.pitcher_id
            if pid not in pitcher_trackers:
                pitcher_trackers[pid] = PitcherCumulative(pitcher_id=pid, name=g.away_starter.name)
            pitcher_trackers[pid].add_start(g.away_starter)
        if g.home_starter.pitcher_id > 0:
            pid = g.home_starter.pitcher_id
            if pid not in pitcher_trackers:
                pitcher_trackers[pid] = PitcherCumulative(pitcher_id=pid, name=g.home_starter.name)
            pitcher_trackers[pid].add_start(g.home_starter)

    print(f"符合條件的對戰: {len(matchups)} 場 (雙方至少 {MIN_GAMES} 場)\n")

    if len(matchups) < 10:
        print("樣本太少，無法做有意義的分析")
        return {}, trackers

    # 要測試的指標
    # (name, getter, direction_for_directional, total_direction)
    # total_direction: "positive" 高者偏大分, "negative" 高者偏小分, "neutral" 不用於大小分
    single_stats = [
        ("win_pct",     lambda s: s['win_pct'],            "higher", "neutral"),
        ("run_diff_pg", lambda s: s['run_diff_per_game'],  "higher", "neutral"),
        ("pyth_pct",    lambda s: s['pyth_pct'],           "higher", "neutral"),
        ("ops",         lambda s: s['ops'],                "higher", "positive"),
        ("slg",         lambda s: s['slg'],                "higher", "positive"),
        ("obp",         lambda s: s['obp'],                "higher", "positive"),
        ("avg",         lambda s: s['avg'],                "higher", "positive"),
        ("runs_pg",     lambda s: s['runs_per_game'],      "higher", "positive"),
        ("hr_pg",       lambda s: s['hr_per_game'],        "higher", "positive"),
        ("bb_rate",     lambda s: s['bb_rate'],            "higher", "positive"),
        ("so_rate",     lambda s: s['so_rate'],            "lower",  "negative"),
        ("era",         lambda s: s['era'],                "lower",  "positive"),
        ("whip",        lambda s: s['whip'],               "lower",  "positive"),
        ("fip",         lambda s: s['fip'],                "lower",  "positive"),
        ("k9",          lambda s: s['k9'],                 "higher", "negative"),
        ("bb9",         lambda s: s['bb9'],                "lower",  "positive"),
        ("k_bb_ratio",  lambda s: s['k_bb_ratio'],         "higher", "negative"),
        ("ra_pg",       lambda s: s['ra_per_game'],        "lower",  "positive"),
        ("bp_fatigue",  lambda s: s['bp_fatigue'],         "lower",  "positive"),
        ("home_adv",    None,                               "home",   "neutral"),
    ]

    # 投手相關指標 (今日先發投手 — 個別累積)
    sp_stats = [
        ("sp_era",    lambda s: s.get('sp_era', 0),    "lower",  "positive"),
        ("sp_whip",   lambda s: s.get('sp_whip', 0),   "lower",  "positive"),
        ("sp_fip",    lambda s: s.get('sp_fip', 0),    "lower",  "positive"),
        ("sp_k9",     lambda s: s.get('sp_k9', 0),     "higher", "negative"),
        ("sp_bb9",    lambda s: s.get('sp_bb9', 0),    "lower",  "positive"),
        ("sp_k_bb",   lambda s: s.get('sp_k_bb', 0),   "higher", "negative"),
        ("sp_hr9",    lambda s: s.get('sp_hr9', 0),    "lower",  "positive"),
    ]

    # 先算各指標的均值和標準差 (必須在此處先算，後面測試需要用)
    all_values = {}
    for m in matchups:
        for snap_key in ['away_snap', 'home_snap']:
            snap = m[snap_key]
            for key in snap:
                if isinstance(snap[key], (int, float)):
                    all_values.setdefault(('team', key), []).append(snap[key])
        for snap_key in ['away_sp_snap', 'home_sp_snap']:
            snap = m.get(snap_key)
            if snap is None:
                continue
            for key in snap:
                if isinstance(snap[key], (int, float)):
                    all_values.setdefault(('sp', key), []).append(snap[key])

    stat_means = {k: sum(v) / len(v) for k, v in all_values.items()}
    stat_stds = {}
    for k, vals in all_values.items():
        mean = stat_means[k]
        variance = sum((x - mean) ** 2 for x in vals) / len(vals) if len(vals) > 1 else 1
        stat_stds[k] = sqrt(variance) if variance > 0 else 1

    # results 結構：results[bet_type][indicator_name] = {correct, total, pct, ...}
    results = {bt[0]: {} for bt in BET_TYPES}

    def test_directional(matchups, stat_name, getter, direction, source, bet_type):
        """測試 directional 指標 (ML, 讓分) 對某 bet type 的命中率"""
        correct = 0
        total = 0
        out_key = f'out_{bet_type}'
        for m in matchups:
            if stat_name == "home_adv":
                predicted = "home"
            else:
                if source == 'team':
                    a_snap = m['away_snap']
                    h_snap = m['home_snap']
                else:
                    a_snap = m['away_sp_snap']
                    h_snap = m['home_sp_snap']
                    if a_snap is None or h_snap is None:
                        continue
                    if a_snap.get('sp_starts', 0) < MIN_STARTS or h_snap.get('sp_starts', 0) < MIN_STARTS:
                        continue

                a = getter(a_snap)
                h = getter(h_snap)
                if a == h:
                    continue
                if direction == "higher":
                    predicted = "away" if a > h else "home"
                else:
                    predicted = "away" if a < h else "home"

            total += 1
            if predicted == m[out_key]:
                correct += 1
        return correct, total

    # 指標名 -> snap 內的 key (用於查 stat_means)
    STAT_KEY_MAP = {
        "ops": "ops", "slg": "slg", "obp": "obp", "avg": "avg",
        "runs_pg": "runs_per_game", "hr_pg": "hr_per_game",
        "bb_rate": "bb_rate", "so_rate": "so_rate",
        "era": "era", "whip": "whip", "fip": "fip",
        "k9": "k9", "bb9": "bb9", "k_bb_ratio": "k_bb_ratio",
        "ra_pg": "ra_per_game", "bp_fatigue": "bp_fatigue",
        "win_pct": "win_pct", "run_diff_pg": "run_diff_per_game", "pyth_pct": "pyth_pct",
        "sp_era": "sp_era", "sp_whip": "sp_whip", "sp_fip": "sp_fip",
        "sp_k9": "sp_k9", "sp_bb9": "sp_bb9", "sp_k_bb": "sp_k_bb", "sp_hr9": "sp_hr9",
    }

    def test_total(matchups, stat_name, getter, total_direction, source, bet_type):
        """測試 total 指標對大小分的命中率 (用 baseline 加總均值預測)"""
        if total_direction == "neutral":
            return 0, 0
        stat_key = STAT_KEY_MAP.get(stat_name, stat_name)
        mean_single = stat_means.get((source, stat_key), 0)
        mean_combined = mean_single * 2

        correct = 0
        total = 0
        out_key = f'out_{bet_type}'

        for m in matchups:
            if source == 'team':
                a_snap = m['away_snap']
                h_snap = m['home_snap']
            else:
                a_snap = m.get('away_sp_snap')
                h_snap = m.get('home_sp_snap')
                if a_snap is None or h_snap is None:
                    continue
                if a_snap.get('sp_starts', 0) < MIN_STARTS or h_snap.get('sp_starts', 0) < MIN_STARTS:
                    continue
            try:
                a = getter(a_snap)
                h = getter(h_snap)
            except (KeyError, TypeError):
                continue
            combined = a + h
            if total_direction == "positive":
                predicted = "over" if combined > mean_combined else "under"
            else:
                predicted = "under" if combined > mean_combined else "over"
            total += 1
            if predicted == m[out_key]:
                correct += 1
        return correct, total

    def add_result(bet_type, stat_name, correct, total, direction_label):
        if total == 0:
            return
        pct = correct / total * 100
        se = sqrt(0.5 * 0.5 / total)
        z = (correct / total - 0.5) / se if se > 0 else 0
        results[bet_type][stat_name] = {
            'correct': correct, 'total': total,
            'pct': round(pct, 1), 'z_score': round(z, 2),
            'significant': abs(z) > 1.96,
            'direction': direction_label,
        }

    # === 為每個 bet type 跑一次所有指標 ===
    for bt_key, bt_name, bt_type, bt_line, _ in BET_TYPES:
        print(f"\n{'='*72}")
        print(f"【{bt_name}】單一指標回測")
        print(f"{'='*72}")
        print(f"{'指標':<16s} | 正確 | 總數 | 命中率  | 方向     | 顯著性")
        print("-" * 72)

        if bt_type == 'directional':
            # 球隊指標
            for stat_name, getter, direction, _ in single_stats:
                c, t = test_directional(matchups, stat_name, getter, direction, 'team', bt_key)
                label = {"higher": "高者勝", "lower": "低者勝", "home": "主場"}[direction]
                add_result(bt_key, stat_name, c, t, label)
                if t > 0:
                    r = results[bt_key][stat_name]
                    sig = "⭐" if r['significant'] else ""
                    print(f"{stat_name:<16s} | {c:5d} | {t:5d} | {r['pct']:5.1f}%  | {label:<8s} | {sig}")

            # 投手指標
            if has_starter_data:
                print(f"  -- 先發投手 --")
                for stat_name, getter, direction, _ in sp_stats:
                    c, t = test_directional(matchups, stat_name, getter, direction, 'sp', bt_key)
                    label = "低者勝" if direction == "lower" else "高者勝"
                    add_result(bt_key, stat_name, c, t, label)
                    if t > 0:
                        r = results[bt_key][stat_name]
                        sig = "⭐" if r['significant'] else ""
                        print(f"{stat_name:<16s} | {c:5d} | {t:5d} | {r['pct']:5.1f}%  | {label:<8s} | {sig}")

        else:  # total
            for stat_name, getter, _, total_dir in single_stats:
                if total_dir == "neutral":
                    continue
                c, t = test_total(matchups, stat_name, getter, total_dir, 'team', bt_key)
                label = "加總高偏大" if total_dir == "positive" else "加總高偏小"
                add_result(bt_key, stat_name, c, t, label)
                if t > 0:
                    r = results[bt_key][stat_name]
                    sig = "⭐" if r['significant'] else ""
                    print(f"{stat_name:<16s} | {c:5d} | {t:5d} | {r['pct']:5.1f}%  | {label:<8s} | {sig}")

            if has_starter_data:
                print(f"  -- 先發投手 --")
                for stat_name, getter, _, total_dir in sp_stats:
                    if total_dir == "neutral":
                        continue
                    c, t = test_total(matchups, stat_name, getter, total_dir, 'sp', bt_key)
                    label = "加總高偏大" if total_dir == "positive" else "加總高偏小"
                    add_result(bt_key, stat_name, c, t, label)
                    if t > 0:
                        r = results[bt_key][stat_name]
                        sig = "⭐" if r['significant'] else ""
                        print(f"{stat_name:<16s} | {c:5d} | {t:5d} | {r['pct']:5.1f}%  | {label:<8s} | {sig}")

    # === 複合指標 (directional: ML, 讓分) ===
    def compute_directional_composite(m, components, use_home_adv):
        score = 0
        for stat_key, weight, source in components:
            if source == "team":
                a_snap = m['away_snap']
                h_snap = m['home_snap']
            else:
                a_snap = m.get('away_sp_snap')
                h_snap = m.get('home_sp_snap')
                if a_snap is None or h_snap is None:
                    return None
                if a_snap.get('sp_starts', 0) < MIN_STARTS or h_snap.get('sp_starts', 0) < MIN_STARTS:
                    return None
            a_val = a_snap.get(stat_key)
            h_val = h_snap.get(stat_key)
            if a_val is None or h_val is None:
                return None
            std = stat_stds.get((source, stat_key), 1) or 1
            mean = stat_means.get((source, stat_key), 0)
            a_z = (a_val - mean) / std
            h_z = (h_val - mean) / std
            score += (h_z - a_z) * weight
        if use_home_adv:
            score += HOME_ADV_BONUS
        return score

    def compute_total_composite(m, components):
        """加總型複合 (大小分用)。回傳 z-score 差距 (相對於樣本均值)"""
        score = 0
        for stat_key, weight, source in components:
            if source == "team":
                a_snap = m['away_snap']
                h_snap = m['home_snap']
            else:
                a_snap = m.get('away_sp_snap')
                h_snap = m.get('home_sp_snap')
                if a_snap is None or h_snap is None:
                    return None
                if a_snap.get('sp_starts', 0) < MIN_STARTS or h_snap.get('sp_starts', 0) < MIN_STARTS:
                    return None
            a_val = a_snap.get(stat_key)
            h_val = h_snap.get(stat_key)
            if a_val is None or h_val is None:
                return None
            combined = a_val + h_val
            mean = stat_means.get((source, stat_key), 0) * 2  # 加總平均
            std = (stat_stds.get((source, stat_key), 1) or 1) * sqrt(2)  # 獨立變量加總的 std
            z = (combined - mean) / std
            score += z * weight
        return score

    for bt_key, bt_name, bt_type, bt_line, _ in BET_TYPES:
        print(f"\n{'='*72}")
        print(f"【{bt_name}】複合指標回測")
        print(f"{'='*72}")
        print(f"{'指標':<30s} | 正確 | 總數 | 命中率  | 顯著性")
        print("-" * 65)

        out_key = f'out_{bt_key}'

        if bt_type == 'directional':
            for comp_name, components, use_home_adv in COMPOSITES_DEF:
                # 讓分類盤口跳過 +home 變體 (10年回測證實主場加成對讓分是負效益)
                if use_home_adv and bt_key != 'ml':
                    continue

                correct = 0
                total = 0
                for m in matchups:
                    score = compute_directional_composite(m, components, use_home_adv)
                    if score is None:
                        continue
                    predicted = "home" if score > 0 else "away"
                    total += 1
                    if predicted == m[out_key]:
                        correct += 1

                if total > 0:
                    comp_stat_name = f"composite:{comp_name}"
                    add_result(bt_key, comp_stat_name, correct, total, "複合")
                    r = results[bt_key][comp_stat_name]
                    sig = "⭐" if r['significant'] else ""
                    print(f"{comp_name:<30s} | {correct:5d} | {total:5d} | {r['pct']:5.1f}%  | {sig}")
        else:  # total
            for comp_name, components in TOTAL_COMPOSITES_DEF:
                correct = 0
                total = 0
                for m in matchups:
                    score = compute_total_composite(m, components)
                    if score is None:
                        continue
                    predicted = "over" if score > 0 else "under"
                    total += 1
                    if predicted == m[out_key]:
                        correct += 1

                if total > 0:
                    comp_stat_name = f"composite:{comp_name}"
                    add_result(bt_key, comp_stat_name, correct, total, "加總複合")
                    r = results[bt_key][comp_stat_name]
                    sig = "⭐" if r['significant'] else ""
                    print(f"{comp_name:<30s} | {correct:5d} | {total:5d} | {r['pct']:5.1f}%  | {sig}")

    # 排名 (各 bet type 分開顯示 top 10)
    for bt_key, bt_name, bt_type, bt_line, _ in BET_TYPES:
        print(f"\n{'='*72}")
        print(f"【{bt_name}】指標排名 Top 15")
        print(f"{'='*72}")
        ranked = sorted(results[bt_key].items(), key=lambda x: -x[1]['pct'])
        for i, (name, r) in enumerate(ranked[:15], 1):
            sig = "⭐" if r.get('significant') else ""
            print(f"  {i:2d}. {name:<32s}  {r['pct']:5.1f}%  ({r['correct']}/{r['total']})  {sig}")

    # === 分層回測 (每個 bet type 各跑一次) ===
    bucket_thresholds_all = {}  # {bet_type: {indicator: thresholds}}
    profitable_filters_all = {}  # {bet_type: [filters]}

    for bt_key, bt_name, bt_type, bt_line, _ in BET_TYPES:
        print(f"\n{'='*72}")
        print(f"【{bt_name}】分層回測")
        print(f"{'='*72}")
        b_thresh, b_profit = run_bucket_analysis_for_bet_type(
            matchups, single_stats, sp_stats, bt_key, bt_type,
            stat_means, stat_stds
        )
        bucket_thresholds_all[bt_key] = b_thresh
        profitable_filters_all[bt_key] = b_profit

    # === 自動存 baseline (多季資料時) ===
    seasons_in_data = sorted(set(int(m['date'][:4]) for m in matchups))
    if len(seasons_in_data) >= 2:
        save_baseline({
            'season_range': f"{seasons_in_data[0]}-{seasons_in_data[-1]}",
            'seasons': seasons_in_data,
            'total_games_analyzed': len(matchups),
            'results': results,
            'stat_means': stat_means,
            'stat_stds': stat_stds,
            'bucket_thresholds': bucket_thresholds_all,
            'profitable_filters': profitable_filters_all,
        })

    return results, trackers


# ═══════════════════════════════════════════
# 分層回測：找出真正能下注的場次
# ═══════════════════════════════════════════
def run_bucket_analysis_for_bet_type(matchups, single_stats, sp_stats, bet_type_key, bet_type,
                                      stat_means, stat_stds):
    """對單一 bet type 做分層回測"""
    STAT_KEY_MAP = {
        "ops": "ops", "slg": "slg", "obp": "obp", "avg": "avg",
        "runs_pg": "runs_per_game", "hr_pg": "hr_per_game",
        "bb_rate": "bb_rate", "so_rate": "so_rate",
        "era": "era", "whip": "whip", "fip": "fip",
        "k9": "k9", "bb9": "bb9", "k_bb_ratio": "k_bb_ratio",
        "ra_pg": "ra_per_game", "bp_fatigue": "bp_fatigue",
        "win_pct": "win_pct", "run_diff_pg": "run_diff_per_game", "pyth_pct": "pyth_pct",
        "sp_era": "sp_era", "sp_whip": "sp_whip", "sp_fip": "sp_fip",
        "sp_k9": "sp_k9", "sp_bb9": "sp_bb9", "sp_k_bb": "sp_k_bb", "sp_hr9": "sp_hr9",
    }
    out_key = f'out_{bet_type_key}'
    print(f"目標: 找出命中率 ≥ 58% 的場次篩選條件\n")

    bucket_targets = []

    # 依 bet type 建立 diff_func
    # diff_func 回傳 (signed_score, predicted_outcome)
    # - directional: score > 0 → home, < 0 → away
    # - total: score > 0 → over, < 0 → under

    if bet_type == 'directional':
        # 單一球隊指標
        for stat_name, getter, direction, _ in single_stats:
            if stat_name == "home_adv":
                continue
            def make_dir_fn(g, d):
                def fn(m):
                    a = g(m['away_snap']); h = g(m['home_snap'])
                    diff = (h - a) if d == "higher" else (a - h)
                    if diff == 0:
                        return None, None
                    return diff, ("home" if diff > 0 else "away")
                return fn
            bucket_targets.append((stat_name, make_dir_fn(getter, direction)))

        # 單一 SP 指標
        for stat_name, getter, direction, _ in sp_stats:
            def make_sp_fn(g, d):
                def fn(m):
                    a_snap = m.get('away_sp_snap'); h_snap = m.get('home_sp_snap')
                    if a_snap is None or h_snap is None:
                        return None, None
                    if a_snap.get('sp_starts', 0) < MIN_STARTS or h_snap.get('sp_starts', 0) < MIN_STARTS:
                        return None, None
                    a = g(a_snap); h = g(h_snap)
                    diff = (h - a) if d == "higher" else (a - h)
                    if diff == 0:
                        return None, None
                    return diff, ("home" if diff > 0 else "away")
                return fn
            bucket_targets.append((stat_name, make_sp_fn(getter, direction)))

        # 複合指標 (directional)
        for comp_name, components, use_home_adv in COMPOSITES_DEF:
            # 讓分類盤口跳過 +home 變體
            if use_home_adv and bet_type_key != 'ml':
                continue
            def make_comp_fn(comps, home_bonus):
                def fn(m):
                    score = 0
                    for stat_key, weight, source in comps:
                        if source == "team":
                            a_snap = m['away_snap']; h_snap = m['home_snap']
                        else:
                            a_snap = m.get('away_sp_snap'); h_snap = m.get('home_sp_snap')
                            if a_snap is None or h_snap is None:
                                return None, None
                            if a_snap.get('sp_starts', 0) < MIN_STARTS or h_snap.get('sp_starts', 0) < MIN_STARTS:
                                return None, None
                        a_val = a_snap.get(stat_key); h_val = h_snap.get(stat_key)
                        if a_val is None or h_val is None:
                            return None, None
                        std = stat_stds.get((source, stat_key), 1) or 1
                        mean = stat_means.get((source, stat_key), 0)
                        score += ((h_val - mean) / std - (a_val - mean) / std) * weight
                    if home_bonus:
                        score += HOME_ADV_BONUS
                    return score, ("home" if score > 0 else "away")
                return fn
            bucket_targets.append((f"comp:{comp_name}", make_comp_fn(components, use_home_adv)))

    else:  # total
        # 單一球隊指標 (總和)
        for stat_name, getter, _, total_dir in single_stats:
            if total_dir == "neutral":
                continue
            stat_key = STAT_KEY_MAP.get(stat_name, stat_name)
            def make_tot_fn(g, td, sk):
                mean_single = stat_means.get(('team', sk), 0)
                std_single = stat_stds.get(('team', sk), 1) or 1
                def fn(m):
                    try:
                        a = g(m['away_snap']); h = g(m['home_snap'])
                    except (KeyError, TypeError):
                        return None, None
                    combined = a + h
                    mean_c = mean_single * 2
                    std_c = std_single * sqrt(2)
                    z = (combined - mean_c) / std_c
                    # 如果 total_direction == negative, 反向 (高值 → 偏小)
                    if td == "negative":
                        z = -z
                    if z == 0:
                        return None, None
                    return z, ("over" if z > 0 else "under")
                return fn
            bucket_targets.append((stat_name, make_tot_fn(getter, total_dir, stat_key)))

        # 單一 SP 指標 (總和)
        for stat_name, getter, _, total_dir in sp_stats:
            if total_dir == "neutral":
                continue
            stat_key = STAT_KEY_MAP.get(stat_name, stat_name)
            def make_sp_tot_fn(g, td, sk):
                mean_single = stat_means.get(('sp', sk), 0)
                std_single = stat_stds.get(('sp', sk), 1) or 1
                def fn(m):
                    a_snap = m.get('away_sp_snap'); h_snap = m.get('home_sp_snap')
                    if a_snap is None or h_snap is None:
                        return None, None
                    if a_snap.get('sp_starts', 0) < MIN_STARTS or h_snap.get('sp_starts', 0) < MIN_STARTS:
                        return None, None
                    try:
                        a = g(a_snap); h = g(h_snap)
                    except (KeyError, TypeError):
                        return None, None
                    combined = a + h
                    mean_c = mean_single * 2
                    std_c = std_single * sqrt(2)
                    z = (combined - mean_c) / std_c
                    if td == "negative":
                        z = -z
                    if z == 0:
                        return None, None
                    return z, ("over" if z > 0 else "under")
                return fn
            bucket_targets.append((stat_name, make_sp_tot_fn(getter, total_dir, stat_key)))

        # 複合指標 (total)
        for comp_name, components in TOTAL_COMPOSITES_DEF:
            def make_tot_comp_fn(comps):
                def fn(m):
                    score = 0
                    for stat_key, weight, source in comps:
                        if source == "team":
                            a_snap = m['away_snap']; h_snap = m['home_snap']
                        else:
                            a_snap = m.get('away_sp_snap'); h_snap = m.get('home_sp_snap')
                            if a_snap is None or h_snap is None:
                                return None, None
                            if a_snap.get('sp_starts', 0) < MIN_STARTS or h_snap.get('sp_starts', 0) < MIN_STARTS:
                                return None, None
                        a_val = a_snap.get(stat_key); h_val = h_snap.get(stat_key)
                        if a_val is None or h_val is None:
                            return None, None
                        combined = a_val + h_val
                        mean_c = stat_means.get((source, stat_key), 0) * 2
                        std_c = (stat_stds.get((source, stat_key), 1) or 1) * sqrt(2)
                        z = (combined - mean_c) / std_c
                        score += z * weight
                    if score == 0:
                        return None, None
                    return score, ("over" if score > 0 else "under")
                return fn
            bucket_targets.append((f"comp:{comp_name}", make_tot_comp_fn(components)))

    bucket_results = {}
    bucket_thresholds = {}
    profitable_filters = []

    for stat_name, diff_func in bucket_targets:
        scored = []
        for m in matchups:
            try:
                result = diff_func(m)
            except (KeyError, TypeError):
                continue
            if result is None or result[0] is None:
                continue
            score, predicted = result
            won = (predicted == m[out_key])
            scored.append((abs(score), won))

        if len(scored) < 50:
            continue

        # 按差距絕對值降序
        scored.sort(key=lambda x: -x[0])

        # 分 5 桶
        n = len(scored)
        bucket_size = n // 5
        buckets = []
        for i in range(5):
            start = i * bucket_size
            end = (i + 1) * bucket_size if i < 4 else n
            bucket = scored[start:end]
            wins = sum(1 for _, w in bucket if w)
            total = len(bucket)
            pct = wins / total * 100 if total > 0 else 0
            buckets.append({
                'bucket': i + 1,
                'wins': wins,
                'total': total,
                'pct': round(pct, 1),
            })

        bucket_results[stat_name] = buckets

        # 記錄各門檻的分數值與命中率
        thresh = {'sample_size': n}
        for top_pct in [5, 10, 20, 30]:
            cutoff = int(n * top_pct / 100)
            if cutoff < 20:
                continue
            top_subset = scored[:cutoff]
            wins = sum(1 for _, w in top_subset if w)
            pct = wins / cutoff * 100
            threshold_val = scored[cutoff - 1][0] if cutoff > 0 else 0  # 最後一個入選的 abs score
            thresh[f'top_{top_pct}_threshold'] = round(threshold_val, 4)
            thresh[f'top_{top_pct}_hit_rate'] = round(pct, 1)
            thresh[f'top_{top_pct}_sample'] = cutoff

            if pct >= 58:
                parlay_rate = (pct / 100) ** 2
                profitable_filters.append({
                    'stat': stat_name,
                    'top_pct': top_pct,
                    'sample': cutoff,
                    'single_pct': round(pct, 1),
                    'parlay_pct': round(parlay_rate * 100, 1),
                    'breakeven_combined_odds': round(1 / parlay_rate, 2),
                    'threshold': round(threshold_val, 4),
                })

        bucket_thresholds[stat_name] = thresh

    # 印出 top 指標的分層結果
    top_stats_for_print = sorted(
        bucket_results.keys(),
        key=lambda k: -bucket_results[k][0]['pct']  # 用 top bucket 排序
    )[:10]

    for stat_name in top_stats_for_print:
        buckets = bucket_results[stat_name]
        top_pct = buckets[0]['pct']
        print(f"  📊 {stat_name}")
        labels = ["Top 20% (差距最大)", "20-40%", "40-60%", "60-80%", "Bottom 20%"]
        for b, label in zip(buckets, labels):
            bar_len = int(b['pct'] / 2)
            bar = "█" * bar_len + "░" * (35 - bar_len)
            marker = " 🎯" if b['pct'] >= 58 else (" ✓" if b['pct'] >= 55 else "")
            print(f"    {label:<22s} {bar} {b['pct']:5.1f}% ({b['wins']}/{b['total']}){marker}")
        print()

    # 印出可獲利篩選條件
    print(f"{'='*72}")
    print(f"⭐ 可正 ROI 的篩選條件 (單腳 ≥58%, 串2關正 EV)")
    print(f"{'='*72}")
    if profitable_filters:
        # 依單腳命中率排序
        profitable_filters.sort(key=lambda x: -x['single_pct'])
        print(f"  {'指標':<30s} | Top% | 樣本 | 單腳%  | 串2關% | 損益平衡")
        print(f"  {'-'*30}-+------+------+--------+--------+---------")
        seen = set()
        for f in profitable_filters[:15]:
            key = (f['stat'], f['top_pct'])
            if key in seen:
                continue
            seen.add(key)
            print(f"  {f['stat']:<30s} | {f['top_pct']:3d}% | {f['sample']:4d} | {f['single_pct']:5.1f}% | {f['parlay_pct']:5.1f}% | {f['breakeven_combined_odds']:.2f}")
        print(f"\n  💡 解讀: 上述「指標 + Top%」的子集場次，命中率達正 EV 門檻")
        print(f"      例: 'comp:ops+era Top 10%' 表示「OPS+ERA 複合指標差距最大的前10% 場次」")
    else:
        print(f"  ⚠️ 沒有任何篩選條件能達到 58% 單腳命中率")
        print(f"     即使分層也難以擺脫台彩抽水")

    # 終極建議
    print(f"\n{'='*72}")
    print(f"💎 終極建議")
    print(f"{'='*72}")
    if profitable_filters:
        best = profitable_filters[0]
        print(f"  最佳單一篩選: {best['stat']} 的 Top {best['top_pct']}% 場次")
        print(f"  → 樣本 {best['sample']} 場, 命中率 {best['single_pct']}%, 串2關 {best['parlay_pct']}%")
        print(f"  → 需要找到合計賠率 > {best['breakeven_combined_odds']} 才有正 EV")
        print(f"  → 每天下注前，先確認對戰是否落在此篩選條件內")
    else:
        print(f"  目前數據顯示沒有任何單一篩選能正 ROI")
        print(f"  建議: 收集更多季數據後再分析")

    return bucket_thresholds, profitable_filters


# ═══════════════════════════════════════════
# Baseline 儲存/載入 (10 年回測基準)
# ═══════════════════════════════════════════
def save_baseline(data):
    """儲存 10 年回測 baseline 到 cache/baseline_10y.json (per bet type 結構)"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, BASELINE_FILE)

    # 為每個 bet type 產生排名
    bet_types_data = {}
    for bt_key, bt_name, bt_type, bt_line, _ in BET_TYPES:
        bt_results = data['results'].get(bt_key, {})
        ranking = []
        for name, r in sorted(bt_results.items(), key=lambda x: -x[1].get('pct', 0)):
            entry = {
                'name': name,
                'pct': r.get('pct'),
                'correct': r.get('correct'),
                'total': r.get('total'),
                'z_score': r.get('z_score'),
                'significant': r.get('significant'),
                'direction': r.get('direction'),
            }
            # 類別標記
            if name.startswith('composite:'):
                entry['category'] = 'composite'
            elif name.startswith('sp_'):
                entry['category'] = 'starter'
            elif name == 'home_adv':
                entry['category'] = 'situational'
            elif name == 'bp_fatigue':
                entry['category'] = 'bullpen'
                entry['warning'] = '10 年大樣本驗證為無效 (≈50%)，僅作參考'
            else:
                entry['category'] = 'team'
            ranking.append(entry)

        bet_types_data[bt_key] = {
            'name': bt_name,
            'type': bt_type,
            'line': bt_line,
            'indicator_ranking': ranking,
            'bucket_thresholds': data['bucket_thresholds'].get(bt_key, {}),
            'profitable_filters': data['profitable_filters'].get(bt_key, []),
        }

    # 序列化 tuple keys → "source:key"
    serialized_means = {f"{k[0]}:{k[1]}": v for k, v in data['stat_means'].items()}
    serialized_stds = {f"{k[0]}:{k[1]}": v for k, v in data['stat_stds'].items()}

    baseline = {
        'generated_at': datetime.now().isoformat(),
        'season_range': data['season_range'],
        'seasons': data['seasons'],
        'total_games_analyzed': data['total_games_analyzed'],
        'bet_types': bet_types_data,
        'stat_means': serialized_means,
        'stat_stds': serialized_stds,
    }

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)

    print(f"\n💾 10 年 baseline 已存入: {path}")
    for bt_key, bt_data in bet_types_data.items():
        n_filters = len(bt_data['profitable_filters'])
        print(f"   {bt_data['name']:<12s}: {len(bt_data['indicator_ranking'])} 指標, {n_filters} 個正 EV 篩選")
def load_baseline():
    """載入 10 年 baseline"""
    path = os.path.join(CACHE_DIR, BASELINE_FILE)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    # 還原 tuple keys
    data['stat_means'] = {tuple(k.split(':', 1)): v for k, v in data['stat_means'].items()}
    data['stat_stds'] = {tuple(k.split(':', 1)): v for k, v in data['stat_stds'].items()}
    return data


# ═══════════════════════════════════════════
# 當季 trackers 建立 (用於 daily)
# ═══════════════════════════════════════════
def build_current_trackers(games, cutoff_date=None):
    """從比賽建立 team + pitcher trackers

    cutoff_date: 只納入此日期「之前」的比賽 (YYYY-MM-DD)
                 None = 納入所有傳入的比賽
    """
    team_trackers = {}
    pitcher_trackers = {}

    games.sort(key=lambda x: (x.date, x.game_pk))

    for g in games:
        # 只納入 cutoff_date 前的比賽 (避免用未來資料)
        if cutoff_date and g.date >= cutoff_date:
            continue

        for name in [g.away_name, g.home_name]:
            if name not in team_trackers:
                team_trackers[name] = TeamCumulative()

        team_trackers[g.away_name].add_game(g.date, g.away_stats, g.winner_side == "away")
        team_trackers[g.home_name].add_game(g.date, g.home_stats, g.winner_side == "home")

        if g.away_starter.pitcher_id > 0:
            pid = g.away_starter.pitcher_id
            if pid not in pitcher_trackers:
                pitcher_trackers[pid] = PitcherCumulative(pitcher_id=pid, name=g.away_starter.name)
            pitcher_trackers[pid].add_start(
                g.away_starter,
                date=g.date,
                vs=g.home_name,
                vs_zh=TEAM_ZH.get(g.home_name, g.home_name),
                team_score=g.away_score,
                opp_score=g.home_score,
                team_won=(g.winner_side == 'away'),
            )
        if g.home_starter.pitcher_id > 0:
            pid = g.home_starter.pitcher_id
            if pid not in pitcher_trackers:
                pitcher_trackers[pid] = PitcherCumulative(pitcher_id=pid, name=g.home_starter.name)
            pitcher_trackers[pid].add_start(
                g.home_starter,
                date=g.date,
                vs=g.away_name,
                vs_zh=TEAM_ZH.get(g.away_name, g.away_name),
                team_score=g.home_score,
                opp_score=g.away_score,
                team_won=(g.winner_side == 'home'),
            )

    return team_trackers, pitcher_trackers


# ═══════════════════════════════════════════
# 第三步：產出今日分析 JSON (使用 10 年 baseline)
# ═══════════════════════════════════════════
def compute_composite_scores_for_bet_type(bt_key, bt_type, away_snap, home_snap,
                                           away_sp_snap, home_sp_snap, baseline,
                                           current_season_means=None):
    """根據 bet_type 算複合指標分數 + bucket 位置

    current_season_means: 大小分用。提供時用「當季均值」取代 baseline 均值做中心點,
                          避免整個聯盟 OPS/ERA 偏移導致永遠預測同一方向。
                          std 仍用 baseline (10 年尺度更穩定)。
    """
    means = baseline['stat_means']
    stds = baseline['stat_stds']
    bt_data = baseline['bet_types'].get(bt_key, {})
    thresholds = bt_data.get('bucket_thresholds', {})

    scores = {}

    if bt_type == 'directional':
        composites = COMPOSITES_DEF
        for comp_name, components, use_home_adv in composites:
            # 讓分類盤口跳過 +home 變體
            if use_home_adv and bt_key != 'ml':
                continue
            score = 0
            valid = True
            for stat_key, weight, source in components:
                if source == "team":
                    a_val = away_snap.get(stat_key)
                    h_val = home_snap.get(stat_key)
                else:
                    if away_sp_snap is None or home_sp_snap is None:
                        valid = False; break
                    if away_sp_snap.get('sp_starts', 0) < MIN_STARTS or home_sp_snap.get('sp_starts', 0) < MIN_STARTS:
                        valid = False; break
                    a_val = away_sp_snap.get(stat_key)
                    h_val = home_sp_snap.get(stat_key)
                if a_val is None or h_val is None:
                    valid = False; break
                mean = means.get((source, stat_key), 0)
                std = stds.get((source, stat_key), 1) or 1
                a_z = (a_val - mean) / std
                h_z = (h_val - mean) / std
                score += (h_z - a_z) * weight
            if not valid:
                continue
            if use_home_adv:
                score += HOME_ADV_BONUS

            full_name = f"comp:{comp_name}"
            t = thresholds.get(full_name, {})
            abs_score = abs(score)
            bucket = None
            if t.get('top_5_threshold') is not None and abs_score >= t['top_5_threshold']:
                bucket = "Top 5%"
            elif t.get('top_10_threshold') is not None and abs_score >= t['top_10_threshold']:
                bucket = "Top 10%"
            elif t.get('top_20_threshold') is not None and abs_score >= t['top_20_threshold']:
                bucket = "Top 20%"
            elif t.get('top_30_threshold') is not None and abs_score >= t['top_30_threshold']:
                bucket = "Top 30%"

            scores[full_name] = {
                'value': round(score, 3),
                'predicts': 'home' if score > 0 else 'away',
                'bucket': bucket,
                'baseline_top_10_hit_rate': t.get('top_10_hit_rate'),
            }

    else:  # total
        composites = TOTAL_COMPOSITES_DEF
        for comp_name, components in composites:
            score = 0
            valid = True
            for stat_key, weight, source in components:
                if source == "team":
                    a_val = away_snap.get(stat_key)
                    h_val = home_snap.get(stat_key)
                else:
                    if away_sp_snap is None or home_sp_snap is None:
                        valid = False; break
                    if away_sp_snap.get('sp_starts', 0) < MIN_STARTS or home_sp_snap.get('sp_starts', 0) < MIN_STARTS:
                        valid = False; break
                    a_val = away_sp_snap.get(stat_key)
                    h_val = home_sp_snap.get(stat_key)
                if a_val is None or h_val is None:
                    valid = False; break
                combined = a_val + h_val
                # 大小分用「當季均值」做中心 (避免整季 OPS/ERA 偏移的系統性偏差)
                # std 仍用 baseline (10 年尺度更穩定)
                if current_season_means:
                    mean_single = current_season_means.get((source, stat_key),
                                    means.get((source, stat_key), 0))
                else:
                    mean_single = means.get((source, stat_key), 0)
                mean_c = mean_single * 2
                std_c = (stds.get((source, stat_key), 1) or 1) * sqrt(2)
                z = (combined - mean_c) / std_c
                score += z * weight
            if not valid:
                continue

            full_name = f"comp:{comp_name}"
            t = thresholds.get(full_name, {})
            abs_score = abs(score)
            bucket = None
            if t.get('top_5_threshold') is not None and abs_score >= t['top_5_threshold']:
                bucket = "Top 5%"
            elif t.get('top_10_threshold') is not None and abs_score >= t['top_10_threshold']:
                bucket = "Top 10%"
            elif t.get('top_20_threshold') is not None and abs_score >= t['top_20_threshold']:
                bucket = "Top 20%"
            elif t.get('top_30_threshold') is not None and abs_score >= t['top_30_threshold']:
                bucket = "Top 30%"

            scores[full_name] = {
                'value': round(score, 3),
                'predicts': 'over' if score > 0 else 'under',
                'bucket': bucket,
                'baseline_top_10_hit_rate': t.get('top_10_hit_rate'),
            }

    return scores


def generate_daily_analysis(games, target_date=None, top5_only=False):
    """daily 主流程：用當季數據 + 10 年 baseline 產出指定日期的比賽分析 JSON

    target_date: 'YYYY-MM-DD' 或 None (預設今天)
    top5_only: True = 只輸出符合 Top 5% 篩選的場次 (開季減噪用)
    """
    today = target_date if target_date else datetime.now().strftime("%Y-%m-%d")
    mode = "Top 5% 嚴選模式" if top5_only else "標準模式"
    print(f"\n=== 比賽分析 ({today}) [{mode}] ===")

    # 載入 10 年 baseline
    baseline = load_baseline()
    if baseline is None:
        print("❌ 找不到 10 年 baseline")
        print("   請先執行: python mlb_analyzer.py correlate range 2015 2025")
        return None

    print(f"  使用 baseline: {baseline['season_range']} ({baseline['total_games_analyzed']} 場)")

    # 建立當季 trackers (只用 target_date 前的比賽, 避免用未來資料)
    team_trackers, pitcher_trackers = build_current_trackers(games, cutoff_date=today)
    print(f"  使用 {today} 前的資料 | 球隊: {len(team_trackers)}, 投手: {len(pitcher_trackers)}")

    # 計算當季均值 (用於大小分 z-score 修正)
    current_season_means = {}
    all_snaps = [t.snapshot(today) for t in team_trackers.values()]
    for key in all_snaps[0]:
        vals = [s[key] for s in all_snaps if isinstance(s[key], (int, float))]
        if vals:
            current_season_means[('team', key)] = sum(vals) / len(vals)
    all_sp_snaps = [p.snapshot() for p in pitcher_trackers.values() if p.starts >= MIN_STARTS]
    if all_sp_snaps:
        for key in all_sp_snaps[0]:
            vals = [s[key] for s in all_sp_snaps if isinstance(s[key], (int, float))]
            if vals:
                current_season_means[('sp', key)] = sum(vals) / len(vals)

    # 抓今日賽程
    sched = api_get(f"/schedule?date={today}&sportId=1&hydrate=probablePitcher,team")
    if not sched or not sched.get('dates'):
        print("  ⚠️ 今天沒有比賽或尚未公布")
        today_matchups = []
    else:
        today_matchups = []
        for game in sched['dates'][0].get('games', []):
            away_team = game['teams']['away']['team']['name']
            home_team = game['teams']['home']['team']['name']

            if away_team not in team_trackers or home_team not in team_trackers:
                continue

            away_snap = team_trackers[away_team].snapshot(today)
            home_snap = team_trackers[home_team].snapshot(today)

            if away_snap['games'] < MIN_GAMES or home_snap['games'] < MIN_GAMES:
                continue

            # 今日先發投手
            away_sp_info = game['teams']['away'].get('probablePitcher', {}) or {}
            home_sp_info = game['teams']['home'].get('probablePitcher', {}) or {}
            away_sp_id = away_sp_info.get('id')
            home_sp_id = home_sp_info.get('id')
            away_sp_name = away_sp_info.get('fullName', 'TBD')
            home_sp_name = home_sp_info.get('fullName', 'TBD')

            away_sp_snap = pitcher_trackers[away_sp_id].snapshot() if away_sp_id in pitcher_trackers else None
            home_sp_snap = pitcher_trackers[home_sp_id].snapshot() if home_sp_id in pitcher_trackers else None

            # 最近 3 場先發明細
            away_sp_recent = pitcher_trackers[away_sp_id].recent_starts(3) if away_sp_id in pitcher_trackers else []
            home_sp_recent = pitcher_trackers[home_sp_id].recent_starts(3) if home_sp_id in pitcher_trackers else []

            # 球隊指標比較 (當季累積值)
            team_comparisons = {}
            team_key_stats = [
                ('ops', 'higher'), ('slg', 'higher'), ('obp', 'higher'), ('avg', 'higher'),
                ('era', 'lower'), ('whip', 'lower'), ('fip', 'lower'),
                ('k9', 'higher'), ('bb9', 'lower'),
                ('run_diff_per_game', 'higher'), ('pyth_pct', 'higher'),
                ('runs_per_game', 'higher'), ('ra_per_game', 'lower'),
                ('bp_fatigue', 'lower'),  # 參考用, 有警告
            ]
            for stat, direction in team_key_stats:
                a = away_snap.get(stat, 0)
                h = home_snap.get(stat, 0)
                if direction == 'higher':
                    edge = 'away' if a > h else 'home' if h > a else 'even'
                else:
                    edge = 'away' if a < h else 'home' if h < a else 'even'
                team_comparisons[stat] = {
                    'away': round(a, 3) if isinstance(a, float) else a,
                    'home': round(h, 3) if isinstance(h, float) else h,
                    'edge': edge,
                }

            # SP 指標比較 (今日先發累積值)
            sp_comparisons = {}
            if away_sp_snap and home_sp_snap:
                sp_key_stats = [
                    ('sp_era', 'lower'), ('sp_whip', 'lower'), ('sp_fip', 'lower'),
                    ('sp_k9', 'higher'), ('sp_bb9', 'lower'), ('sp_k_bb', 'higher'),
                ]
                for stat, direction in sp_key_stats:
                    a = away_sp_snap.get(stat, 0)
                    h = home_sp_snap.get(stat, 0)
                    if direction == 'higher':
                        edge = 'away' if a > h else 'home' if h > a else 'even'
                    else:
                        edge = 'away' if a < h else 'home' if h < a else 'even'
                    sp_comparisons[stat] = {
                        'away': round(a, 3),
                        'home': round(h, 3),
                        'edge': edge,
                    }
                sp_comparisons['sp_starts'] = {
                    'away': away_sp_snap.get('sp_starts', 0),
                    'home': home_sp_snap.get('sp_starts', 0),
                    'edge': 'even',
                }

            # === 計算預期總分 (用於大小分方向判定) ===
            est_total = away_snap['runs_per_game'] + home_snap['runs_per_game']

            # SP 修正：今日先發比球隊平均差 → 對手多得分
            if away_sp_snap and away_sp_snap.get('sp_starts', 0) >= MIN_STARTS:
                team_era = away_snap.get('era', 4.0)
                sp_era = away_sp_snap.get('sp_era', team_era)
                est_total += (sp_era - team_era) * 0.15  # 客隊 SP 差 → 主隊多得分
            if home_sp_snap and home_sp_snap.get('sp_starts', 0) >= MIN_STARTS:
                team_era = home_snap.get('era', 4.0)
                sp_era = home_sp_snap.get('sp_era', team_era)
                est_total += (sp_era - team_era) * 0.15  # 主隊 SP 差 → 客隊多得分

            est_total = round(est_total, 2)

            # 預先合併大小分的 profitable_filters (同一組 z-score 共享篩選)
            total_bt_keys = [k for k, _, t, _, _ in BET_TYPES if t == 'total']
            total_filters_union = {}  # {(stat, top_pct): filter_dict}
            for tk in total_bt_keys:
                for f in baseline['bet_types'].get(tk, {}).get('profitable_filters', []):
                    key = (f['stat'], f['top_pct'])
                    if key not in total_filters_union or f['single_pct'] > total_filters_union[key]['single_pct']:
                        total_filters_union[key] = f

            # 同樣合併 bucket_thresholds (取各盤口中最寬鬆的門檻)
            total_thresholds_merged = {}
            for tk in total_bt_keys:
                bt_thresh = baseline['bet_types'].get(tk, {}).get('bucket_thresholds', {})
                for stat_name, thresh in bt_thresh.items():
                    if stat_name not in total_thresholds_merged:
                        total_thresholds_merged[stat_name] = dict(thresh)
                    else:
                        existing = total_thresholds_merged[stat_name]
                        for pct_key in ['top_5_threshold', 'top_10_threshold', 'top_20_threshold', 'top_30_threshold']:
                            if pct_key in thresh:
                                if pct_key not in existing or thresh[pct_key] < existing[pct_key]:
                                    existing[pct_key] = thresh[pct_key]  # 取較寬鬆的門檻

            # 對每個 bet type 計算複合分數 + 推薦
            bet_type_analysis = {}
            any_top_10 = False

            for bt_key, bt_name, bt_type, bt_line, _ in BET_TYPES:
                bt_data = baseline['bet_types'].get(bt_key, {})
                scores = compute_composite_scores_for_bet_type(
                    bt_key, bt_type, away_snap, home_snap,
                    away_sp_snap, home_sp_snap, baseline,
                    current_season_means=current_season_means if bt_type == 'total' else None
                )

                # 大小分盤口: 用共享的 filters 和 thresholds
                if bt_type == 'total':
                    filters_to_check = list(total_filters_union.values())
                    # 用共享門檻重新判定 bucket (覆蓋 compute_composite 的結果)
                    for score_name, score_data in scores.items():
                        t = total_thresholds_merged.get(score_name, {})
                        abs_score = abs(score_data['value'])
                        if t.get('top_5_threshold') is not None and abs_score >= t['top_5_threshold']:
                            score_data['bucket'] = "Top 5%"
                        elif t.get('top_10_threshold') is not None and abs_score >= t['top_10_threshold']:
                            score_data['bucket'] = "Top 10%"
                        elif t.get('top_20_threshold') is not None and abs_score >= t['top_20_threshold']:
                            score_data['bucket'] = "Top 20%"
                        elif t.get('top_30_threshold') is not None and abs_score >= t['top_30_threshold']:
                            score_data['bucket'] = "Top 30%"
                        else:
                            score_data['bucket'] = None
                else:
                    filters_to_check = bt_data.get('profitable_filters', [])

                # 符合 profitable filter 的檢查
                matched_filters = []
                for f in filters_to_check:
                    stat_name = f['stat']
                    top_pct = f['top_pct']
                    score_data = scores.get(stat_name)
                    if score_data and score_data.get('bucket'):
                        bucket_pct = int(score_data['bucket'].replace('Top ', '').replace('%', ''))
                        if bucket_pct <= top_pct:
                            matched_filters.append({
                                'stat': stat_name,
                                'top_pct': top_pct,
                                'baseline_hit_rate': f['single_pct'],
                                'min_odds_needed': f['breakeven_combined_odds'],
                            })

                # === Top 5%/10% 判定 ===
                # 方向型盤口 (ML, 讓分): 統一用不讓分的主力指標
                #   → 因為「贏」是基本條件，讓分 1.5/2.5 的信心不應超過不讓分
                # 大小分盤口: 用該盤口自己的主力指標
                is_top_5_pct = False
                is_top_10_pct = False

                if bt_type == 'directional':
                    # 統一用不讓分 (ml) 的主力指標
                    ml_filters = baseline['bet_types'].get('ml', {}).get('profitable_filters', [])
                    if ml_filters:
                        ml_primary = max(ml_filters, key=lambda f: f.get('single_pct', 0))
                        primary_score = scores.get(ml_primary['stat'])
                        if primary_score and primary_score.get('bucket'):
                            bucket_pct = int(primary_score['bucket'].replace('Top ', '').replace('%', ''))
                            is_top_5_pct = bucket_pct <= 5
                            is_top_10_pct = bucket_pct <= 10
                else:
                    # 大小分: 用自己的主力指標
                    if filters_to_check:
                        primary_filter = max(filters_to_check, key=lambda f: f.get('single_pct', 0))
                        primary_score = scores.get(primary_filter['stat'])
                        if primary_score and primary_score.get('bucket'):
                            bucket_pct = int(primary_score['bucket'].replace('Top ', '').replace('%', ''))
                            is_top_5_pct = bucket_pct <= 5
                            is_top_10_pct = bucket_pct <= 10

                if is_top_10_pct:
                    any_top_10 = True

                # === 方向判定 ===
                strong_scores = [
                    s for s in scores.values()
                    if s.get('bucket') in ('Top 5%', 'Top 10%', 'Top 20%')
                ]
                if not strong_scores:
                    strong_scores = list(scores.values())

                if bt_type == 'directional':
                    # 不讓分/讓分: 用 z-score 加權投票
                    home_z_sum = sum(s['value'] for s in strong_scores if s['value'] > 0)
                    away_z_sum = sum(-s['value'] for s in strong_scores if s['value'] < 0)
                    home_count = sum(1 for s in strong_scores if s['predicts'] == 'home')
                    away_count = sum(1 for s in strong_scores if s['predicts'] == 'away')

                    if home_z_sum > away_z_sum:
                        predicted = 'home'
                    elif away_z_sum > home_z_sum:
                        predicted = 'away'
                    else:
                        predicted = 'even'
                    vote_info = {
                        'home_count': home_count, 'away_count': away_count,
                        'home_z_sum': round(home_z_sum, 2),
                        'away_z_sum': round(away_z_sum, 2),
                    }
                    total_z = home_z_sum + away_z_sum
                    minority_z = min(home_z_sum, away_z_sum)
                else:
                    # === 大小分: 用「預期總分 vs 盤口線」判定方向 ===
                    # est_total 已在上方從 runs_per_game + SP 修正計算
                    distance = est_total - bt_line  # 正 = 偏大, 負 = 偏小
                    if distance > 0:
                        predicted = 'over'
                    elif distance < 0:
                        predicted = 'under'
                    else:
                        predicted = 'even'

                    # 複合 z-score 仍計算 (用於信心度和 bucket)
                    over_z_sum = sum(s['value'] for s in strong_scores if s['value'] > 0)
                    under_z_sum = sum(-s['value'] for s in strong_scores if s['value'] < 0)
                    over_count = sum(1 for s in strong_scores if s['predicts'] == 'over')
                    under_count = sum(1 for s in strong_scores if s['predicts'] == 'under')

                    vote_info = {
                        'est_total': est_total,
                        'line': bt_line,
                        'distance': round(distance, 2),
                        'over_count': over_count, 'under_count': under_count,
                        'over_z_sum': round(over_z_sum, 2),
                        'under_z_sum': round(under_z_sum, 2),
                    }
                    total_z = over_z_sum + under_z_sum
                    minority_z = min(over_z_sum, under_z_sum)

                # === 訊號衝突偵測 ===
                # 弱勢方 z-sum 佔總 z-sum 的比例
                minority_ratio = minority_z / total_z if total_z > 0 else 0
                signal_conflict = minority_ratio >= 0.35  # 35%+ = 輕度衝突
                severe_conflict = minority_ratio >= 0.45  # 45%+ = 嚴重衝突
                vote_info['minority_ratio'] = round(minority_ratio, 2)
                vote_info['conflict'] = severe_conflict

                # === 信心度計算（考慮訊號衝突）===
                if is_top_10_pct and len(matched_filters) >= 2 and not signal_conflict:
                    confidence = 'high'
                elif is_top_10_pct and not severe_conflict:
                    confidence = 'medium'
                elif any(f['top_pct'] <= 20 for f in matched_filters) and not severe_conflict:
                    confidence = 'medium'
                else:
                    confidence = 'low'

                # 嚴重衝突時直接降到 low
                if severe_conflict:
                    confidence = 'low'

                min_odds_needed = min((f['min_odds_needed'] for f in matched_filters), default=None)

                bet_type_analysis[bt_key] = {
                    'name': bt_name,
                    'line': bt_line,
                    'composite_scores': scores,
                    'matched_filters': matched_filters,
                    'is_top_5_pct': is_top_5_pct,
                    'is_top_10_pct': is_top_10_pct,
                    'predicted': predicted,
                    'votes': vote_info,
                    'confidence': confidence,
                    'signal_conflict': signal_conflict,
                    'min_combined_odds_needed': min_odds_needed,
                }

            # 計算優勢數 (for display only)
            all_comparisons = {**team_comparisons, **sp_comparisons}
            away_edges = sum(1 for v in all_comparisons.values() if v['edge'] == 'away')
            home_edges = sum(1 for v in all_comparisons.values() if v['edge'] == 'home')

            away_zh = TEAM_ZH.get(away_team, away_team)
            home_zh = TEAM_ZH.get(home_team, home_team)

            any_top_10 = any(bt.get('is_top_10_pct') for bt in bet_type_analysis.values())
            any_top_5 = any(bt.get('is_top_5_pct') for bt in bet_type_analysis.values())

            matchup_data = {
                'away': away_team, 'away_zh': away_zh,
                'home': home_team, 'home_zh': home_zh,
                'away_sp': away_sp_name, 'home_sp': home_sp_name,
                'away_record': f"{away_snap['wins']}-{away_snap['losses']}",
                'home_record': f"{home_snap['wins']}-{home_snap['losses']}",
                'away_sp_recent': away_sp_recent,
                'home_sp_recent': home_sp_recent,
                'team_comparisons': team_comparisons,
                'sp_comparisons': sp_comparisons,
                'away_edges': away_edges,
                'home_edges': home_edges,
                'any_top_5_pct': any_top_5,
                'any_top_10_pct': any_top_10,
                'bet_types': bet_type_analysis,
            }

            today_matchups.append(matchup_data)

        # 排序: Top 5% > Top 10% > 其餘
        today_matchups.sort(key=lambda m: (
            0 if m['any_top_5_pct'] else (1 if m['any_top_10_pct'] else 2),
            -abs(m['away_edges'] - m['home_edges'])
        ))

    # 輸出 JSON
    current_games_count = sum(1 for g in games if int(g.date[:4]) == CURRENT_SEASON)
    output = {
        'generated_at': datetime.now().isoformat(),
        'date': today,
        'current_season': CURRENT_SEASON,
        'current_season_games': current_games_count,
        'top5_mode': top5_only,
        'baseline': {
            'season_range': baseline['season_range'],
            'total_games_analyzed': baseline['total_games_analyzed'],
            'bet_types': {
                bt_key: {
                    'name': bt_data['name'],
                    'type': bt_data['type'],
                    'line': bt_data['line'],
                    'indicator_ranking': bt_data['indicator_ranking'],
                    'profitable_filters': bt_data['profitable_filters'],
                }
                for bt_key, bt_data in baseline['bet_types'].items()
            },
        },
        'today_matchups': today_matchups,
    }

    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ANALYSIS_OUTPUT)
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 印出摘要
    print(f"\n  共 {len(today_matchups)} 場符合條件")
    for m in today_matchups:
        marker = " ⭐" if m['any_top_10_pct'] else ""
        print(f"\n  {m['away_zh']} ({m['away_record']}) @ {m['home_zh']} ({m['home_record']}){marker}")
        print(f"    先發: {m['away_sp']} vs {m['home_sp']}")
        for bt_key, bt_rec in m['bet_types'].items():
            if bt_rec['predicted'] == 'even':
                continue
            pred_display = bt_rec['predicted']
            if bt_rec['predicted'] in ('home', 'away'):
                pred_display = m['home_zh'] if bt_rec['predicted'] == 'home' else m['away_zh']
            elif bt_rec['predicted'] == 'over':
                pred_display = '大'
            elif bt_rec['predicted'] == 'under':
                pred_display = '小'
            top10_mark = " ⭐" if bt_rec['is_top_10_pct'] else ""
            print(f"    [{bt_rec['name']}] → {pred_display} (信心 {bt_rec['confidence']}){top10_mark}")
            for f in bt_rec['matched_filters'][:2]:
                print(f"       • {f['stat']} Top {f['top_pct']}% (歷史 {f['baseline_hit_rate']}%, 最低賠率 {f['min_odds_needed']})")

    print(f"\n分析已存入: {out_file}")
    return output


# ═══════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()

    # fetch <season>
    if cmd == "fetch":
        if len(sys.argv) < 3:
            print("用法: fetch <season>  例: fetch 2024")
            sys.exit(1)
        season = int(sys.argv[2])
        fetch_season_games(season)
        # 抓完直接回測該季
        games = load_cached_games(season)
        if games:
            run_correlation_analysis(games)

    # rebuild <season> 或 rebuild range <s> <e>  — 從既有 box cache 重新解析
    elif cmd == "rebuild":
        if len(sys.argv) < 3:
            print("用法: rebuild <season>  或  rebuild range <s> <e>")
            sys.exit(1)
        if sys.argv[2] == "range":
            if len(sys.argv) < 5:
                print("用法: rebuild range <start> <end>")
                sys.exit(1)
            start_y = int(sys.argv[3])
            end_y = int(sys.argv[4])
            seasons = [y for y in range(start_y, end_y + 1) if y not in EXCLUDED_SEASONS]
            for y in seasons:
                rebuild_season(y)
        else:
            rebuild_season(int(sys.argv[2]))

    # range <start> <end>
    elif cmd == "range":
        if len(sys.argv) < 4:
            print("用法: range <start_year> <end_year>  例: range 2015 2025")
            sys.exit(1)
        start_y = int(sys.argv[2])
        end_y = int(sys.argv[3])
        fetch_season_range(start_y, end_y)
        # 抓完直接回測整段 (跳過 EXCLUDED_SEASONS)
        seasons = [y for y in range(start_y, end_y + 1) if y not in EXCLUDED_SEASONS]
        games = load_cached_games(seasons)
        if games:
            run_correlation_analysis(games)

    # correlate <season>  或  correlate range <s> <e>
    elif cmd == "correlate":
        if len(sys.argv) < 3:
            print("用法: correlate <season>  或  correlate range <s> <e>")
            sys.exit(1)

        if sys.argv[2] == "range":
            if len(sys.argv) < 5:
                print("用法: correlate range <start> <end>")
                sys.exit(1)
            seasons = [y for y in range(int(sys.argv[3]), int(sys.argv[4]) + 1) if y not in EXCLUDED_SEASONS]
        else:
            seasons = [int(sys.argv[2])]

        games = load_cached_games(seasons)
        if games:
            run_correlation_analysis(games)

    # today - 用既有當季數據產生指定日期分析 (不打 API 抓新資料)
    elif cmd == "today":
        args = [a for a in sys.argv[2:] if not a.startswith('--')]
        top5 = '--top5' in sys.argv
        target = parse_date_arg(args[0]) if args else None
        games = load_cached_games(CURRENT_SEASON)
        if games:
            generate_daily_analysis(games, target_date=target, top5_only=top5)

    # daily - 每日例行: 抓當季最新 + 產出指定日期分析 JSON
    elif cmd == "daily":
        args = [a for a in sys.argv[2:] if not a.startswith('--')]
        top5 = '--top5' in sys.argv
        target = parse_date_arg(args[0]) if args else None
        fetch_season_games(CURRENT_SEASON)
        games = load_cached_games(CURRENT_SEASON)
        if games:
            generate_daily_analysis(games, target_date=target, top5_only=top5)

    else:
        print(f"未知指令: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
