"""
MLB 投注分析工具 — 統計相關性回測 + 牛棚追蹤 + 每日分析
用法:
  python mlb_analyzer.py fetch                  # 從 MLB API 抓取全部賽季數據並快取
  python mlb_analyzer.py correlate              # 用快取數據做相關性回測
  python mlb_analyzer.py today                  # 產出今日比賽分析 JSON
  python mlb_analyzer.py all                    # 以上全部執行
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
SEASON = 2026
SEASON_START = "2026-03-25"
MIN_GAMES = 5          # 至少打幾場才納入分析
BULLPEN_WINDOW = 3     # 牛棚疲勞追蹤天數
FIP_CONSTANT = 3.10    # 近似 cFIP
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
API_BASE = "https://statsapi.mlb.com/api/v1"

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
# API 工具
# ═══════════════════════════════════════════
def api_get(path):
    url = f"{API_BASE}{path}"
    try:
        req = Request(url, headers={"User-Agent": "MLB-Analyzer/1.0"})
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        print(f"  API error {e.code}: {url}")
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
def fetch_season_games():
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"games_{SEASON}.json")

    # 決定日期範圍
    today = datetime.now().strftime("%Y-%m-%d")
    start = SEASON_START

    print(f"正在從 MLB API 抓取 {start} ~ {today} 的所有比賽...")

    all_games = []
    current = datetime.strptime(start, "%Y-%m-%d")
    end = datetime.strptime(today, "%Y-%m-%d")

    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        print(f"  {date_str}...", end=" ", flush=True)

        sched = api_get(f"/schedule?date={date_str}&sportId=1&hydrate=linescore")
        if not sched or not sched.get('dates'):
            print("無比賽")
            current += timedelta(days=1)
            continue

        day_games = []
        for game_data in sched['dates'][0].get('games', []):
            status = game_data.get('status', {}).get('abstractGameState', '')
            if status != 'Final':
                continue
            day_games.append(game_data['gamePk'])

        print(f"{len(day_games)} 場", end="", flush=True)

        for gpk in day_games:
            # 檢查是否已快取
            box_cache = os.path.join(CACHE_DIR, f"box_{gpk}.json")
            if os.path.exists(box_cache):
                with open(box_cache, 'r') as f:
                    box = json.load(f)
            else:
                box = api_get(f"/game/{gpk}/boxscore")
                if box:
                    with open(box_cache, 'w') as f:
                        json.dump(box, f)
                    time.sleep(0.3)  # 避免打太快
                else:
                    continue

            game = parse_boxscore(box, gpk, date_str)
            if game:
                all_games.append(game)

        print(f" ✓")
        current += timedelta(days=1)

    # 儲存解析後的結果
    serializable = []
    for g in all_games:
        d = {
            'date': g.date, 'game_pk': g.game_pk,
            'away_name': g.away_name, 'home_name': g.home_name,
            'away_score': g.away_score, 'home_score': g.home_score,
            'winner_side': g.winner_side,
            'away_stats': asdict(g.away_stats),
            'home_stats': asdict(g.home_stats),
        }
        serializable.append(d)

    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)

    print(f"\n完成！共 {len(all_games)} 場比賽已存入 {cache_file}")
    return all_games


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
        )
        return game
    except Exception as e:
        print(f"\n  解析 {game_pk} 失敗: {e}")
        return None


def load_cached_games():
    cache_file = os.path.join(CACHE_DIR, f"games_{SEASON}.json")
    if not os.path.exists(cache_file):
        print("找不到快取，請先執行 fetch")
        return []

    with open(cache_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    games = []
    for d in data:
        g = GameRecord(
            date=d['date'], game_pk=d['game_pk'],
            away_name=d['away_name'], home_name=d['home_name'],
            away_score=d['away_score'], home_score=d['home_score'],
            winner_side=d['winner_side'],
            away_stats=TeamGameStats(**d['away_stats']),
            home_stats=TeamGameStats(**d['home_stats']),
        )
        games.append(g)

    games.sort(key=lambda x: (x.date, x.game_pk))
    return games


# ═══════════════════════════════════════════
# 第二步：相關性回測
# ═══════════════════════════════════════════
def run_correlation_analysis(games):
    print(f"\n=== 相關性回測 ({len(games)} 場) ===\n")

    # 按日期排序
    games.sort(key=lambda x: (x.date, x.game_pk))

    # 累積追蹤器
    trackers = {}

    # 收集 matchup 數據
    matchups = []

    for g in games:
        for name in [g.away_name, g.home_name]:
            if name not in trackers:
                trackers[name] = TeamCumulative()

        # 取得賽前快照
        away_snap = trackers[g.away_name].snapshot(g.date)
        home_snap = trackers[g.home_name].snapshot(g.date)

        # 只有雙方都打了足夠場次才納入
        if away_snap['games'] >= MIN_GAMES and home_snap['games'] >= MIN_GAMES:
            matchups.append({
                'date': g.date,
                'away': g.away_name,
                'home': g.home_name,
                'away_snap': away_snap,
                'home_snap': home_snap,
                'winner_side': g.winner_side,
            })

        # 更新累積（在記錄 matchup 之後！）
        trackers[g.away_name].add_game(g.date, g.away_stats, g.winner_side == "away")
        trackers[g.home_name].add_game(g.date, g.home_stats, g.winner_side == "home")

    print(f"符合條件的對戰: {len(matchups)} 場 (雙方至少 {MIN_GAMES} 場)\n")

    if len(matchups) < 10:
        print("樣本太少，無法做有意義的分析")
        return {}, trackers

    # 要測試的指標
    single_stats = [
        # (指標名, 取值函數, 方向: "higher"=高的好 / "lower"=低的好)
        ("win_pct", lambda s: s['win_pct'], "higher"),
        ("run_diff_pg", lambda s: s['run_diff_per_game'], "higher"),
        ("pyth_pct", lambda s: s['pyth_pct'], "higher"),
        ("ops", lambda s: s['ops'], "higher"),
        ("slg", lambda s: s['slg'], "higher"),
        ("obp", lambda s: s['obp'], "higher"),
        ("avg", lambda s: s['avg'], "higher"),
        ("runs_pg", lambda s: s['runs_per_game'], "higher"),
        ("hr_pg", lambda s: s['hr_per_game'], "higher"),
        ("bb_rate", lambda s: s['bb_rate'], "higher"),
        ("so_rate", lambda s: s['so_rate'], "lower"),   # 低三振率較好
        ("era", lambda s: s['era'], "lower"),
        ("whip", lambda s: s['whip'], "lower"),
        ("fip", lambda s: s['fip'], "lower"),
        ("k9", lambda s: s['k9'], "higher"),
        ("bb9", lambda s: s['bb9'], "lower"),
        ("k_bb_ratio", lambda s: s['k_bb_ratio'], "higher"),
        ("ra_pg", lambda s: s['ra_per_game'], "lower"),
        ("bp_fatigue", lambda s: s['bp_fatigue'], "lower"),  # 低疲勞較好
        ("home_adv", None, "home"),  # 特殊：永遠選主場
    ]

    results = {}

    print(f"{'指標':<16s} | 正確 | 總數 | 命中率  | 方向     | 顯著性")
    print("-" * 72)

    for stat_name, getter, direction in single_stats:
        correct = 0
        total = 0

        for m in matchups:
            if stat_name == "home_adv":
                predicted_winner = "home"
            else:
                away_val = getter(m['away_snap'])
                home_val = getter(m['home_snap'])

                if away_val == home_val:
                    continue

                if direction == "higher":
                    predicted_winner = "away" if away_val > home_val else "home"
                else:  # lower is better
                    predicted_winner = "away" if away_val < home_val else "home"

            total += 1
            if predicted_winner == m['winner_side']:
                correct += 1

        if total > 0:
            pct = correct / total * 100
            # 二項檢定 p-value (近似)
            p = correct / total
            se = sqrt(0.5 * 0.5 / total)  # H0: p=0.5
            z = (p - 0.5) / se if se > 0 else 0
            significant = "⭐" if abs(z) > 1.96 else ""
            direction_label = {"higher": "高者勝", "lower": "低者勝", "home": "主場"}[direction]

            results[stat_name] = {
                'correct': correct, 'total': total,
                'pct': round(pct, 1), 'z_score': round(z, 2),
                'significant': abs(z) > 1.96,
                'direction': direction_label,
            }

            print(f"{stat_name:<16s} | {correct:3d}  | {total:3d}  | {pct:5.1f}%  | {direction_label:<8s} | {significant}")

    # === 複合指標 ===
    print(f"\n{'='*72}")
    print(f"複合指標")
    print(f"{'='*72}")
    print(f"{'指標':<30s} | 正確 | 總數 | 命中率  | 顯著性")
    print("-" * 65)

    composites = [
        ("ops+era", [("ops", 1), ("era", -1)]),
        ("ops+fip", [("ops", 1), ("fip", -1)]),
        ("ops+whip", [("ops", 1), ("whip", -1)]),
        ("run_diff+bp_fatigue", [("run_diff_per_game", 1), ("bp_fatigue", -1)]),
        ("ops+era+bp_fatigue", [("ops", 1), ("era", -1), ("bp_fatigue", -1)]),
        ("pyth+ops+fip", [("pyth_pct", 1), ("ops", 1), ("fip", -1)]),
        ("run_diff+fip", [("run_diff_per_game", 1), ("fip", -1)]),
        ("slg+era", [("slg", 1), ("era", -1)]),
        ("obp+whip", [("obp", 1), ("whip", -1)]),
        ("run_diff+ops+era+bp", [("run_diff_per_game", 1), ("ops", 0.5), ("era", -0.5), ("bp_fatigue", -0.5)]),
    ]

    # 先算各指標的均值和標準差 (用於 z-score 正規化)
    all_values = {}
    for m in matchups:
        for snap_key in ['away_snap', 'home_snap']:
            snap = m[snap_key]
            for key in snap:
                if isinstance(snap[key], (int, float)):
                    all_values.setdefault(key, []).append(snap[key])

    stat_means = {k: sum(v) / len(v) for k, v in all_values.items()}
    stat_stds = {}
    for k, vals in all_values.items():
        mean = stat_means[k]
        variance = sum((x - mean) ** 2 for x in vals) / len(vals) if len(vals) > 1 else 1
        stat_stds[k] = sqrt(variance) if variance > 0 else 1

    for comp_name, components in composites:
        correct = 0
        total = 0

        for m in matchups:
            score = 0
            valid = True
            for stat_key, weight in components:
                a_val = m['away_snap'].get(stat_key)
                h_val = m['home_snap'].get(stat_key)
                if a_val is None or h_val is None:
                    valid = False
                    break
                std = stat_stds.get(stat_key, 1)
                if std == 0:
                    std = 1
                a_z = (a_val - stat_means.get(stat_key, 0)) / std
                h_z = (h_val - stat_means.get(stat_key, 0)) / std
                score += (h_z - a_z) * weight  # 正 = 主場優勢

            if not valid:
                continue

            predicted = "home" if score > 0 else "away"
            total += 1
            if predicted == m['winner_side']:
                correct += 1

        if total > 0:
            pct = correct / total * 100
            se = sqrt(0.5 * 0.5 / total)
            z = (correct / total - 0.5) / se if se > 0 else 0
            sig = "⭐" if abs(z) > 1.96 else ""

            results[f"composite:{comp_name}"] = {
                'correct': correct, 'total': total,
                'pct': round(pct, 1), 'z_score': round(z, 2),
                'significant': abs(z) > 1.96,
                'components': comp_name,
            }

            print(f"{comp_name:<30s} | {correct:3d}  | {total:3d}  | {pct:5.1f}%  | {sig}")

    # 排名
    print(f"\n{'='*72}")
    print(f"指標排名（依命中率排序）")
    print(f"{'='*72}")
    ranked = sorted(results.items(), key=lambda x: -x[1]['pct'])
    for i, (name, r) in enumerate(ranked, 1):
        sig = "⭐" if r.get('significant') else ""
        print(f"  {i:2d}. {name:<30s}  {r['pct']:5.1f}%  ({r['correct']}/{r['total']})  {sig}")

    return results, trackers


# ═══════════════════════════════════════════
# 第三步：今日比賽分析
# ═══════════════════════════════════════════
def generate_today_analysis(games, correlations, trackers):
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n=== 今日比賽分析 ({today}) ===")

    sched = api_get(f"/schedule?date={today}&sportId=1&hydrate=probablePitcher,linescore,team")
    if not sched or not sched.get('dates'):
        print("今天沒有比賽或尚未公布")
        return

    today_matchups = []
    for game in sched['dates'][0].get('games', []):
        away_team = game['teams']['away']['team']['name']
        home_team = game['teams']['home']['team']['name']

        away_sp = game['teams']['away'].get('probablePitcher', {}).get('fullName', 'TBD')
        home_sp = game['teams']['home'].get('probablePitcher', {}).get('fullName', 'TBD')

        away_snap = trackers[away_team].snapshot(today) if away_team in trackers else None
        home_snap = trackers[home_team].snapshot(today) if home_team in trackers else None

        if not away_snap or not home_snap:
            continue

        # 比較關鍵指標
        comparisons = {}
        key_stats = ['ops', 'slg', 'obp', 'era', 'whip', 'fip', 'k9',
                     'run_diff_per_game', 'pyth_pct', 'bp_fatigue']
        for stat in key_stats:
            a = away_snap.get(stat, 0)
            h = home_snap.get(stat, 0)
            # 判斷誰有優勢
            better_higher = stat in ['ops', 'slg', 'obp', 'k9', 'run_diff_per_game', 'pyth_pct']
            if better_higher:
                edge = "away" if a > h else "home" if h > a else "even"
            else:
                edge = "away" if a < h else "home" if h < a else "even"
            comparisons[stat] = {
                'away': round(a, 3), 'home': round(h, 3), 'edge': edge
            }

        # 計算優勢數
        away_edges = sum(1 for v in comparisons.values() if v['edge'] == 'away')
        home_edges = sum(1 for v in comparisons.values() if v['edge'] == 'home')

        away_zh = TEAM_ZH.get(away_team, away_team)
        home_zh = TEAM_ZH.get(home_team, home_team)

        today_matchups.append({
            'away': away_team, 'away_zh': away_zh,
            'home': home_team, 'home_zh': home_zh,
            'away_sp': away_sp, 'home_sp': home_sp,
            'away_record': f"{away_snap['wins']}-{away_snap['losses']}",
            'home_record': f"{home_snap['wins']}-{home_snap['losses']}",
            'comparisons': comparisons,
            'away_edges': away_edges,
            'home_edges': home_edges,
            'edge_summary': "away" if away_edges > home_edges else "home" if home_edges > away_edges else "even",
        })

    # 輸出 JSON
    output = {
        'generated_at': datetime.now().isoformat(),
        'date': today,
        'total_season_games': len(games),
        'correlations': correlations,
        'matchups': today_matchups,
    }

    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mlb_analysis.json")
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 印出摘要
    for m in today_matchups:
        away_zh = m['away_zh']
        home_zh = m['home_zh']
        print(f"\n  {away_zh} ({m['away_record']}) @ {home_zh} ({m['home_record']})")
        print(f"  先發: {m['away_sp']} vs {m['home_sp']}")
        print(f"  指標優勢: {away_zh} {m['away_edges']}項 vs {home_zh} {m['home_edges']}項")

        for stat, comp in m['comparisons'].items():
            arrow = "◀" if comp['edge'] == 'away' else "▶" if comp['edge'] == 'home' else "="
            print(f"    {stat:<18s}  {comp['away']:.3f}  {arrow}  {comp['home']:.3f}")

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

    if cmd in ("fetch", "all"):
        fetch_season_games()

    if cmd in ("correlate", "all"):
        games = load_cached_games()
        if games:
            correlations, trackers = run_correlation_analysis(games)

            if cmd == "all" or (len(sys.argv) > 2 and sys.argv[2] == "--today"):
                generate_today_analysis(games, correlations, trackers)

    if cmd == "today":
        games = load_cached_games()
        if games:
            correlations, trackers = run_correlation_analysis(games)
            generate_today_analysis(games, correlations, trackers)

    if cmd not in ("fetch", "correlate", "today", "all"):
        print(f"未知指令: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
