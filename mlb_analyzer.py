"""
MLB 投注分析工具 — 統計相關性回測 + 牛棚追蹤 + 每日分析
用法:
  python mlb_analyzer.py fetch <season>              # 抓單一賽季 (例: fetch 2024)
  python mlb_analyzer.py range <start> <end>         # 抓多季 (例: range 2023 2026)
  python mlb_analyzer.py rebuild <season>            # 從既有 box cache 重新解析 (不打 API)
  python mlb_analyzer.py rebuild range <s> <e>       # 多季重新解析
  python mlb_analyzer.py correlate <season>          # 回測單一賽季
  python mlb_analyzer.py correlate range <s> <e>     # 回測多季合併
  python mlb_analyzer.py today                       # 產出今日分析 JSON (用當季數據)
  python mlb_analyzer.py daily                       # 抓當季最新+回測+今日分析(每日例行)
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
MIN_STARTS = 3         # 投手至少先發幾場才納入分析
BULLPEN_WINDOW = 3     # 牛棚疲勞追蹤天數
FIP_CONSTANT = 3.10    # 近似 cFIP
HOME_ADV_BONUS = 0.15  # 主場優勢加成 (z-score 單位，約對應 1-2% 勝率)
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

    def add_start(self, s: StarterStats):
        self.starts += 1
        self.outs += s.outs
        self.h += s.h
        self.er += s.er
        self.bb += s.bb
        self.so += s.so
        self.hr += s.hr
        self.pitches += s.pitches

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

    # 儲存
    parsed.sort(key=lambda x: (x['date'], x['game_pk']))
    cache_file = os.path.join(CACHE_DIR, f"games_{season}.json")
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)

    print(f"  完成！{season} 共 {len(parsed)} 場 (缺 {missing} 場 box cache)")
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

        for d in data:
            g = GameRecord(
                date=d['date'], game_pk=d['game_pk'],
                away_name=d['away_name'], home_name=d['home_name'],
                away_score=d['away_score'], home_score=d['home_score'],
                winner_side=d['winner_side'],
                away_stats=TeamGameStats(**d['away_stats']),
                home_stats=TeamGameStats(**d['home_stats']),
                away_starter=StarterStats(**d['away_starter']) if 'away_starter' in d else StarterStats(),
                home_starter=StarterStats(**d['home_starter']) if 'home_starter' in d else StarterStats(),
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
            matchups.append({
                'date': g.date,
                'season': game_season,
                'away': g.away_name,
                'home': g.home_name,
                'away_snap': away_snap,
                'home_snap': home_snap,
                'away_sp_snap': away_sp_snap,
                'home_sp_snap': home_sp_snap,
                'winner_side': g.winner_side,
            })

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
        ("so_rate", lambda s: s['so_rate'], "lower"),
        ("era", lambda s: s['era'], "lower"),
        ("whip", lambda s: s['whip'], "lower"),
        ("fip", lambda s: s['fip'], "lower"),
        ("k9", lambda s: s['k9'], "higher"),
        ("bb9", lambda s: s['bb9'], "lower"),
        ("k_bb_ratio", lambda s: s['k_bb_ratio'], "higher"),
        ("ra_pg", lambda s: s['ra_per_game'], "lower"),
        ("bp_fatigue", lambda s: s['bp_fatigue'], "lower"),
        ("home_adv", None, "home"),
    ]

    # 投手相關指標 (今日先發投手 — 個別累積)
    sp_stats = [
        ("sp_era", lambda s: s.get('sp_era', 0), "lower"),
        ("sp_whip", lambda s: s.get('sp_whip', 0), "lower"),
        ("sp_fip", lambda s: s.get('sp_fip', 0), "lower"),
        ("sp_k9", lambda s: s.get('sp_k9', 0), "higher"),
        ("sp_bb9", lambda s: s.get('sp_bb9', 0), "lower"),
        ("sp_k_bb", lambda s: s.get('sp_k_bb', 0), "higher"),
        ("sp_hr9", lambda s: s.get('sp_hr9', 0), "lower"),
    ]

    results = {}

    print(f"{'指標':<16s} | 正確 | 總數 | 命中率  | 方向     | 顯著性")
    print("-" * 72)

    def test_indicator(stat_name, getter, direction, snap_key='snap'):
        """測試單一指標的命中率"""
        correct = 0
        total = 0

        for m in matchups:
            if stat_name == "home_adv":
                predicted_winner = "home"
            else:
                if snap_key == 'snap':
                    a_snap = m['away_snap']
                    h_snap = m['home_snap']
                else:  # sp_snap
                    a_snap = m['away_sp_snap']
                    h_snap = m['home_sp_snap']

                if a_snap is None or h_snap is None:
                    continue

                # 對 SP 指標檢查最低先發場數
                if snap_key == 'sp_snap':
                    if a_snap.get('sp_starts', 0) < MIN_STARTS or h_snap.get('sp_starts', 0) < MIN_STARTS:
                        continue

                away_val = getter(a_snap)
                home_val = getter(h_snap)

                if away_val == home_val:
                    continue

                if direction == "higher":
                    predicted_winner = "away" if away_val > home_val else "home"
                else:
                    predicted_winner = "away" if away_val < home_val else "home"

            total += 1
            if predicted_winner == m['winner_side']:
                correct += 1
        return correct, total

    for stat_name, getter, direction in single_stats:
        correct, total = test_indicator(stat_name, getter, direction, 'snap')

        if total > 0:
            pct = correct / total * 100
            p = correct / total
            se = sqrt(0.5 * 0.5 / total)
            z = (p - 0.5) / se if se > 0 else 0
            significant = "⭐" if abs(z) > 1.96 else ""
            direction_label = {"higher": "高者勝", "lower": "低者勝", "home": "主場"}[direction]

            results[stat_name] = {
                'correct': correct, 'total': total,
                'pct': round(pct, 1), 'z_score': round(z, 2),
                'significant': abs(z) > 1.96,
                'direction': direction_label,
            }
            print(f"{stat_name:<16s} | {correct:5d} | {total:5d} | {pct:5.1f}%  | {direction_label:<8s} | {significant}")

    # === 投手指標 (今日先發 vs 對手) ===
    if has_starter_data:
        print(f"\n{'='*72}")
        print(f"投手指標（今日先發投手累積）")
        print(f"{'='*72}")
        print(f"{'指標':<16s} | 正確 | 總數 | 命中率  | 方向     | 顯著性")
        print("-" * 72)

        for stat_name, getter, direction in sp_stats:
            correct, total = test_indicator(stat_name, getter, direction, 'sp_snap')

            if total > 0:
                pct = correct / total * 100
                se = sqrt(0.5 * 0.5 / total)
                z = (pct/100 - 0.5) / se if se > 0 else 0
                significant = "⭐" if abs(z) > 1.96 else ""
                direction_label = "低者勝" if direction == "lower" else "高者勝"

                results[stat_name] = {
                    'correct': correct, 'total': total,
                    'pct': round(pct, 1), 'z_score': round(z, 2),
                    'significant': abs(z) > 1.96,
                    'direction': direction_label,
                }
                print(f"{stat_name:<16s} | {correct:5d} | {total:5d} | {pct:5.1f}%  | {direction_label:<8s} | {significant}")

    # === 複合指標 ===
    print(f"\n{'='*72}")
    print(f"複合指標")
    print(f"{'='*72}")
    print(f"{'指標':<30s} | 正確 | 總數 | 命中率  | 顯著性")
    print("-" * 65)

    # 複合指標：(name, [(stat_key, weight, source)], use_home_adv)
    # source: "team" 用球隊累積，"sp" 用今日先發投手
    composites = [
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

        # === 用今日先發投手 (替代球隊投手) ===
        ("ops+sp_era", [("ops", 1, "team"), ("sp_era", -1, "sp")], False),
        ("ops+sp_fip", [("ops", 1, "team"), ("sp_fip", -1, "sp")], False),
        ("ops+sp_whip", [("ops", 1, "team"), ("sp_whip", -1, "sp")], False),
        ("run_diff+sp_fip", [("run_diff_per_game", 1, "team"), ("sp_fip", -1, "sp")], False),
        ("pyth+ops+sp_fip", [("pyth_pct", 1, "team"), ("ops", 1, "team"), ("sp_fip", -1, "sp")], False),

        # === SP + 主場優勢 (組合最完整) ===
        ("ops+sp_era+home", [("ops", 1, "team"), ("sp_era", -1, "sp")], True),
        ("ops+sp_fip+home", [("ops", 1, "team"), ("sp_fip", -1, "sp")], True),
        ("pyth+ops+sp_fip+home", [("pyth_pct", 1, "team"), ("ops", 1, "team"), ("sp_fip", -1, "sp")], True),
        ("run_diff+sp_fip+home", [("run_diff_per_game", 1, "team"), ("sp_fip", -1, "sp")], True),
    ]

    # 先算各指標的均值和標準差 (用於 z-score 正規化) - 球隊與SP分開
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

    def get_snap(m, source, side):
        if source == "team":
            return m[f'{side}_snap']
        else:
            return m.get(f'{side}_sp_snap')

    for comp_name, components, use_home_adv in composites:
        correct = 0
        total = 0

        for m in matchups:
            score = 0
            valid = True
            for stat_key, weight, source in components:
                a_snap = get_snap(m, source, 'away')
                h_snap = get_snap(m, source, 'home')
                if a_snap is None or h_snap is None:
                    valid = False
                    break

                # SP 指標檢查最低先發場數
                if source == "sp":
                    if a_snap.get('sp_starts', 0) < MIN_STARTS or h_snap.get('sp_starts', 0) < MIN_STARTS:
                        valid = False
                        break

                a_val = a_snap.get(stat_key)
                h_val = h_snap.get(stat_key)
                if a_val is None or h_val is None:
                    valid = False
                    break

                std = stat_stds.get((source, stat_key), 1) or 1
                mean = stat_means.get((source, stat_key), 0)
                a_z = (a_val - mean) / std
                h_z = (h_val - mean) / std
                score += (h_z - a_z) * weight  # 正 = 主場優勢

            if not valid:
                continue

            # 加入主場優勢
            if use_home_adv:
                score += HOME_ADV_BONUS

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

            print(f"{comp_name:<30s} | {correct:5d} | {total:5d} | {pct:5.1f}%  | {sig}")

    # 排名
    print(f"\n{'='*72}")
    print(f"指標排名（依命中率排序）")
    print(f"{'='*72}")
    ranked = sorted(results.items(), key=lambda x: -x[1]['pct'])
    for i, (name, r) in enumerate(ranked, 1):
        sig = "⭐" if r.get('significant') else ""
        print(f"  {i:2d}. {name:<30s}  {r['pct']:5.1f}%  ({r['correct']}/{r['total']})  {sig}")

    # === 分層回測 (核心功能) ===
    bucket_results = run_bucket_analysis(matchups, single_stats, sp_stats, composites, stat_means, stat_stds)
    results['_buckets'] = bucket_results

    return results, trackers


# ═══════════════════════════════════════════
# 分層回測：找出真正能下注的場次
# ═══════════════════════════════════════════
def run_bucket_analysis(matchups, single_stats, sp_stats, composites, stat_means, stat_stds):
    """
    對每個指標做分層分析:
    - 按「指標差距大小」排序所有場次
    - 分成 5 桶 (Top 20%, 20-40%, ...)
    - 找出命中率 > 58% 的場次組合 (才有正 EV)
    """
    print(f"\n{'='*72}")
    print(f"分層回測 — 找出真正能下注的場次")
    print(f"{'='*72}")
    print(f"目標: 找出命中率 ≥ 58% 的場次篩選條件 (才能在台彩串2關正 ROI)\n")

    bucket_targets = []

    # 單一球隊指標
    for stat_name, getter, direction in single_stats:
        if stat_name == "home_adv":
            continue

        def make_diff_func(g, d):
            def diff_func(m):
                a = g(m['away_snap'])
                h = g(m['home_snap'])
                return (h - a) if d == "higher" else (a - h)
            return diff_func

        bucket_targets.append((stat_name, make_diff_func(getter, direction)))

    # 單一 SP 指標
    for stat_name, getter, direction in sp_stats:
        def make_sp_diff_func(g, d):
            def diff_func(m):
                a_snap = m.get('away_sp_snap')
                h_snap = m.get('home_sp_snap')
                if a_snap is None or h_snap is None:
                    return None
                if a_snap.get('sp_starts', 0) < MIN_STARTS or h_snap.get('sp_starts', 0) < MIN_STARTS:
                    return None
                a = g(a_snap)
                h = g(h_snap)
                return (h - a) if d == "higher" else (a - h)
            return diff_func

        bucket_targets.append((stat_name, make_sp_diff_func(getter, direction)))

    # 複合指標
    for comp_name, components, use_home_adv in composites:
        def make_composite_func(comps, home_bonus):
            def diff_func(m):
                score = 0
                for stat_key, weight, source in comps:
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
                if home_bonus:
                    score += HOME_ADV_BONUS
                return score
            return diff_func

        bucket_targets.append((f"comp:{comp_name}", make_composite_func(components, use_home_adv)))

    bucket_results = {}
    profitable_filters = []  # 蒐集 >58% 的篩選條件

    for stat_name, diff_func in bucket_targets:
        # 計算每場比賽的 signed diff
        scored = []
        for m in matchups:
            try:
                diff = diff_func(m)
            except (KeyError, TypeError):
                continue
            if diff is None or diff == 0:
                continue
            predicted = "home" if diff > 0 else "away"
            won = (predicted == m['winner_side'])
            scored.append((abs(diff), won))

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

        # 收集 >58% 的子集 (累積前 N%)
        for top_pct in [10, 20, 30]:
            cutoff = int(n * top_pct / 100)
            if cutoff < 30:
                continue
            top_subset = scored[:cutoff]
            wins = sum(1 for _, w in top_subset if w)
            pct = wins / cutoff * 100
            if pct >= 58:
                # 串2關 ROI 估算
                parlay_rate = (pct / 100) ** 2
                profitable_filters.append({
                    'stat': stat_name,
                    'top_pct': top_pct,
                    'sample': cutoff,
                    'single_pct': round(pct, 1),
                    'parlay_pct': round(parlay_rate * 100, 1),
                    'breakeven_combined_odds': round(1 / parlay_rate, 2),
                })

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

    return bucket_results


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

    # today - 用當季數據產生今日分析
    elif cmd == "today":
        games = load_cached_games(CURRENT_SEASON)
        if games:
            correlations, trackers = run_correlation_analysis(games)
            generate_today_analysis(games, correlations, trackers)

    # daily - 每日例行: 抓當季最新+回測+今日分析
    elif cmd == "daily":
        fetch_season_games(CURRENT_SEASON)
        games = load_cached_games(CURRENT_SEASON)
        if games:
            correlations, trackers = run_correlation_analysis(games)
            generate_today_analysis(games, correlations, trackers)

    else:
        print(f"未知指令: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
