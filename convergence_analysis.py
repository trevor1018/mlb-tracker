"""
收斂分析 — 看各指標在「球隊打幾場」/「投手先發幾場」後開始穩定
用法: python convergence_analysis.py
"""
import sys
from mlb_analyzer import (
    load_cached_games, TeamCumulative, PitcherCumulative,
    EXCLUDED_SEASONS
)

# 載入 10 年資料
seasons = [y for y in range(2015, 2026) if y not in EXCLUDED_SEASONS]
print(f"載入 {len(seasons)} 季資料...")
games = load_cached_games(seasons)
print(f"共 {len(games)} 場\n")

# 按日期排序
games.sort(key=lambda x: (x.date, x.game_pk))

# 逐場收集「賽前快照 + 當下樣本數」
trackers = {}
pitcher_trackers = {}
current_season = None
matchups = []

for g in games:
    season = int(g.date[:4])
    if season != current_season:
        trackers = {}
        pitcher_trackers = {}
        current_season = season

    for name in [g.away_name, g.home_name]:
        if name not in trackers:
            trackers[name] = TeamCumulative()

    away_snap = trackers[g.away_name].snapshot(g.date)
    home_snap = trackers[g.home_name].snapshot(g.date)

    away_sp_snap = None
    home_sp_snap = None
    if g.away_starter.pitcher_id > 0 and g.away_starter.pitcher_id in pitcher_trackers:
        away_sp_snap = pitcher_trackers[g.away_starter.pitcher_id].snapshot()
    if g.home_starter.pitcher_id > 0 and g.home_starter.pitcher_id in pitcher_trackers:
        home_sp_snap = pitcher_trackers[g.home_starter.pitcher_id].snapshot()

    if away_snap['games'] >= 1 and home_snap['games'] >= 1:
        min_team_games = min(away_snap['games'], home_snap['games'])
        min_sp_starts = None
        if away_sp_snap and home_sp_snap:
            min_sp_starts = min(away_sp_snap['sp_starts'], home_sp_snap['sp_starts'])

        matchups.append({
            'min_team_games': min_team_games,
            'min_sp_starts': min_sp_starts,
            'away_snap': away_snap,
            'home_snap': home_snap,
            'away_sp_snap': away_sp_snap,
            'home_sp_snap': home_sp_snap,
            'winner': g.winner_side,
        })

    trackers[g.away_name].add_game(g.date, g.away_stats, g.winner_side == 'away')
    trackers[g.home_name].add_game(g.date, g.home_stats, g.winner_side == 'home')

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

print(f"收集了 {len(matchups)} 組對戰 matchups (雙方至少 1 場)\n")


def test_indicator(subset, getter, direction):
    correct = 0
    total = 0
    for m in subset:
        a = getter(m['away_snap']) if getter.__code__.co_varnames[0] == 's' else None
        # Fallback: use snap dict directly
        pass

    for m in subset:
        try:
            a = getter(m['away_snap'])
            h = getter(m['home_snap'])
        except (KeyError, TypeError):
            continue
        if a == h:
            continue
        if direction == 'higher':
            pred = 'away' if a > h else 'home'
        else:
            pred = 'away' if a < h else 'home'
        total += 1
        if pred == m['winner']:
            correct += 1
    return correct, total


def test_sp_indicator(subset, getter, direction):
    correct = 0
    total = 0
    for m in subset:
        if m['away_sp_snap'] is None or m['home_sp_snap'] is None:
            continue
        try:
            a = getter(m['away_sp_snap'])
            h = getter(m['home_sp_snap'])
        except (KeyError, TypeError):
            continue
        if a == h:
            continue
        if direction == 'higher':
            pred = 'away' if a > h else 'home'
        else:
            pred = 'away' if a < h else 'home'
        total += 1
        if pred == m['winner']:
            correct += 1
    return correct, total


# === 球隊指標收斂分析 ===
print("=" * 90)
print("球隊指標收斂分析 — 按「雙方球隊都至少打 N 場」分桶")
print("=" * 90)

team_buckets = [
    (1, 4),
    (5, 9),
    (10, 14),
    (15, 19),
    (20, 29),
    (30, 49),
    (50, 79),
    (80, 119),
    (120, 200),
]

team_indicators = [
    ('win_pct',     lambda s: s['win_pct'],             'higher'),
    ('pyth_pct',    lambda s: s['pyth_pct'],            'higher'),
    ('run_diff_pg', lambda s: s['run_diff_per_game'],   'higher'),
    ('ops',         lambda s: s['ops'],                  'higher'),
    ('era',         lambda s: s['era'],                  'lower'),
    ('fip',         lambda s: s['fip'],                  'lower'),
    ('whip',        lambda s: s['whip'],                 'lower'),
    ('k9',          lambda s: s['k9'],                   'higher'),
]

# 印表頭
print(f"\n{'球隊場數':<12s} {'樣本數':<8s}", end='')
for name, _, _ in team_indicators:
    print(f"{name:<11s}", end='')
print()
print("-" * 95)

for low, high in team_buckets:
    bucket = [m for m in matchups if low <= m['min_team_games'] <= high]
    if len(bucket) < 50:
        continue
    label = f"{low}-{high}"
    print(f"{label:<12s} {len(bucket):<8d}", end='')
    for name, getter, direction in team_indicators:
        c, t = test_indicator(bucket, getter, direction)
        pct = c / t * 100 if t > 0 else 0
        print(f"{pct:5.1f}%     ", end='')
    print()


# === SP 指標收斂分析 ===
print("\n")
print("=" * 90)
print("先發投手指標收斂分析 — 按「雙方 SP 都先發 N 場」分桶")
print("=" * 90)

sp_buckets = [
    (1, 1),
    (2, 2),
    (3, 4),
    (5, 7),
    (8, 12),
    (13, 20),
    (21, 50),
]

sp_indicators = [
    ('sp_era',   lambda s: s['sp_era'],   'lower'),
    ('sp_fip',   lambda s: s['sp_fip'],   'lower'),
    ('sp_whip',  lambda s: s['sp_whip'],  'lower'),
    ('sp_k9',    lambda s: s['sp_k9'],    'higher'),
    ('sp_bb9',   lambda s: s['sp_bb9'],   'lower'),
    ('sp_k_bb',  lambda s: s['sp_k_bb'],  'higher'),
]

print(f"\n{'SP 先發場數':<14s} {'樣本數':<8s}", end='')
for name, _, _ in sp_indicators:
    print(f"{name:<11s}", end='')
print()
print("-" * 95)

for low, high in sp_buckets:
    bucket = [m for m in matchups if m['min_sp_starts'] is not None and low <= m['min_sp_starts'] <= high]
    if len(bucket) < 50:
        continue
    label = f"{low}-{high}"
    print(f"{label:<14s} {len(bucket):<8d}", end='')
    for name, getter, direction in sp_indicators:
        c, t = test_sp_indicator(bucket, getter, direction)
        pct = c / t * 100 if t > 0 else 0
        print(f"{pct:5.1f}%     ", end='')
    print()


# === 加上「季節階段」分析 (按 3 等分) ===
print("\n")
print("=" * 90)
print("季節階段分析 — 將每季的比賽按時間分成 3 等分")
print("=" * 90)

# 每個 matchup 大概落在賽季哪個階段 (依 min_team_games / 162)
def season_phase(min_games):
    """回傳 '開季', '賽中', '後段'"""
    if min_games < 20:
        return '開季 (1-19 場)'
    elif min_games < 55:
        return '賽中早期 (20-54 場)'
    elif min_games < 100:
        return '賽中後期 (55-99 場)'
    else:
        return '後段 (100+ 場)'


phases = ['開季 (1-19 場)', '賽中早期 (20-54 場)', '賽中後期 (55-99 場)', '後段 (100+ 場)']

print(f"\n{'階段':<22s} {'樣本數':<8s}", end='')
for name, _, _ in team_indicators[:6]:
    print(f"{name:<11s}", end='')
print()
print("-" * 90)

for phase in phases:
    bucket = [m for m in matchups if season_phase(m['min_team_games']) == phase]
    if len(bucket) < 50:
        continue
    print(f"{phase:<22s} {len(bucket):<8d}", end='')
    for name, getter, direction in team_indicators[:6]:
        c, t = test_indicator(bucket, getter, direction)
        pct = c / t * 100 if t > 0 else 0
        print(f"{pct:5.1f}%     ", end='')
    print()

print("\n完成!")
