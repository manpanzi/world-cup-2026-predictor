#!/usr/bin/env python3
"""
World Cup 2026 Match Prediction & WeChat Work Alert System.
ELO ratings + recent form + live betting odds → match analysis.
"""

import base64
import hashlib
import io
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from itertools import combinations

import requests
from matplotlib import font_manager
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# Fix Windows console encoding for emoji output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------- config ----------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
MATCHES_FILE = os.path.join(DATA_DIR, "worldcup_raw.json")
ELO_FILE = os.path.join(DATA_DIR, "elo_ratings.json")
FORM_FILE = os.path.join(DATA_DIR, "recent_form.json")

WEBHOOK_URL = os.getenv(
    "WECHAT_WEBHOOK_URL",
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=bce95ffc-b9c3-482f-bd15-1c764f4c7892",
)

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
PUSHPLUS_TOPIC = os.getenv("PUSHPLUS_TOPIC", "")

CST = timezone(timedelta(hours=8))

SPORTTERY_API = "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"

# sporttery.cn team name → our internal name
SPORTTERY_TEAM_MAP = {
    "墨西哥": "Mexico", "南非": "South Africa", "韩国": "South Korea",
    "捷克": "Czech Republic", "加拿大": "Canada", "波黑": "Bosnia & Herzegovina",
    "卡塔尔": "Qatar", "瑞士": "Switzerland", "巴西": "Brazil", "摩洛哥": "Morocco",
    "海地": "Haiti", "苏格兰": "Scotland", "美国": "USA",
    "土耳其": "Turkey", "澳大利亚": "Australia", "巴拉圭": "Paraguay",
    "德国": "Germany", "厄瓜多尔": "Ecuador", "科特迪瓦": "Ivory Coast",
    "库拉索": "Curacao", "荷兰": "Netherlands", "日本": "Japan",
    "瑞典": "Sweden", "突尼斯": "Tunisia", "比利时": "Belgium", "伊朗": "Iran",
    "埃及": "Egypt", "新西兰": "New Zealand", "西班牙": "Spain",
    "乌拉圭": "Uruguay", "佛得角": "Cape Verde", "沙特": "Saudi Arabia",
    "沙特阿拉伯": "Saudi Arabia", "法国": "France", "挪威": "Norway",
    "塞内加尔": "Senegal", "伊拉克": "Iraq",
    "阿根廷": "Argentina", "奥地利": "Austria", "约旦": "Jordan",
    "阿尔及利": "Algeria", "亚": "Algeria",
    "葡萄牙": "Portugal", "哥伦比亚": "Colombia", "乌兹别克": "Uzbekistan",
    "乌兹别克斯坦": "Uzbekistan", "刚果": "DR Congo", "刚果(金)": "DR Congo",
    "英格兰": "England", "克罗地亚": "Croatia",
    "巴拿马": "Panama", "加纳": "Ghana",
}

# ---------- data loading ----------


def load_data():
    with open(MATCHES_FILE, encoding="utf-8") as f:
        matches_data = json.load(f)
    with open(ELO_FILE, encoding="utf-8") as f:
        elo_data = json.load(f)
    with open(FORM_FILE, encoding="utf-8") as f:
        form_data = json.load(f)
    return matches_data["matches"], elo_data, form_data


def resolve_elo(name, elo_data):
    name_map = elo_data.get("name_map", {})
    resolved = name_map.get(name, name)
    return elo_data["teams"].get(resolved)


def resolve_form(name, elo_data, form_data):
    name_map = elo_data.get("name_map", {})
    resolved = name_map.get(name, name)
    return form_data["forms"].get(resolved, "?-?-?-?-?")

# ---------- sporttery.cn API ----------


def fetch_sporttery_odds():
    """Fetch live 竞彩 SP odds from sporttery.cn. Returns {} on failure."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Referer": "https://m.sporttery.cn/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        resp = requests.get(SPORTTERY_API,
                            params={"poolCode": "hhad,had", "channel": "c"},
                            headers=headers, timeout=10)
        data = resp.json()
        matches = data.get("value", {}).get("matchInfoList", [])
        odds_lookup = {}
        for day in matches:
            for sub in day.get("subMatchList", []):
                key = (sub.get("homeTeamAllName", ""), sub.get("awayTeamAbbName", ""))
                had_odds = None
                hhad_odds = None
                # Check top-level HAD/HHAD objects
                for pool_code, pool_key in [("HAD", "had"), ("HHAD", "hhad")]:
                    obj = sub.get(pool_key, {})
                    if obj and obj.get("h"):
                        gl_str = obj.get("goalLine", "0")
                        try:
                            gl = int(float(gl_str))
                        except (ValueError, TypeError):
                            gl = 0
                        if pool_code == "HAD":
                            had_odds = {"h": float(obj["h"]), "d": float(obj["d"]),
                                         "a": float(obj["a"])}
                        else:
                            hhad_odds = {"h": float(obj["h"]), "d": float(obj["d"]),
                                          "a": float(obj["a"]), "line": gl}
                # Fallback: check oddsList
                for o in sub.get("oddsList", []):
                    pc = o.get("poolCode", "")
                    if pc == "HAD" and had_odds is None:
                        had_odds = {"h": float(o["h"]), "d": float(o["d"]),
                                     "a": float(o["a"])}
                    elif pc == "HHAD" and hhad_odds is None:
                        gl_str = o.get("goalLine", "0")
                        try:
                            gl = int(float(gl_str))
                        except (ValueError, TypeError):
                            gl = 0
                        hhad_odds = {"h": float(o["h"]), "d": float(o["d"]),
                                      "a": float(o["a"]), "line": gl}
                match_num = sub.get("matchNum", "")
                odds_lookup[key] = {"had": had_odds, "hhad": hhad_odds, "num": match_num}
        return odds_lookup
    except Exception as e:
        print(f"  [WARN] sporttery API: {e}")
        return {}


def resolve_sporttery_team(name_en):
    """Reverse-map our internal team name to sporttery.cn Chinese name."""
    for cn_name, en_name in SPORTTERY_TEAM_MAP.items():
        if en_name == name_en:
            return cn_name
    return None


# ---------- 24h time window ----------


def get_matches_in_window(all_matches):
    """
    Return matches whose Beijing kickoff falls in [yesterday 21:00 CST, today 21:00 CST].
    """
    now_cst = datetime.now(CST)
    yesterday = (now_cst - timedelta(days=1)).date()
    window_start = datetime(yesterday.year, yesterday.month, yesterday.day,
                            21, 0, 0, tzinfo=CST)
    if now_cst >= window_start + timedelta(hours=24):
        window_start = window_start + timedelta(days=1)  # if past today's 21:00, use today
    window_end = window_start + timedelta(hours=24)

    # Calculate match_date_str for display
    if window_start.date() == window_end.date():
        match_date_str = window_start.strftime("%m月%d日")
    else:
        match_date_str = (f"{window_start.strftime('%m月%d日')}~"
                          f"{window_end.strftime('%m月%d日')}")

    result = []
    for m in all_matches:
        bj_time, _ = to_beijing_time(m.get("time", ""))
        hh, mm = map(int, bj_time.split(":"))
        match_date_local = datetime.strptime(m["date"], "%Y-%m-%d").date()
        match_dt_cst = datetime(match_date_local.year, match_date_local.month,
                                match_date_local.day, hh, mm, tzinfo=CST)
        if window_start <= match_dt_cst < window_end:
            result.append(m)

    return result, match_date_str


# ---------- Beijing time ----------


def to_beijing_time(time_str):
    """
    Parse '13:00 UTC-6' → Beijing time string like '03:00 (次日)'.
    Returns (display_time, is_next_day).
    """
    m = re.match(r"(\d{1,2}):(\d{2})\s*UTC([+-]\d+)", time_str)
    if not m:
        return time_str, False

    hh, mm = int(m.group(1)), int(m.group(2))
    utc_offset = int(m.group(3))

    # Convert to UTC first, then to CST (UTC+8)
    utc_minutes = hh * 60 + mm - utc_offset * 60
    bj_minutes = utc_minutes + 8 * 60

    bj_minutes %= 24 * 60
    bj_hh, bj_mm = divmod(bj_minutes, 60)
    next_day = bj_hh < 6  # if Beijing time is before 6am, likely next day

    return f"{bj_hh:02d}:{bj_mm:02d}", next_day


# ---------- prediction engine ----------


def elo_win_probability(elo_a, elo_b):
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))


def calc_match_probabilities(elo_a, elo_b):
    e_a = elo_win_probability(elo_a, elo_b)
    diff = abs(elo_a - elo_b)
    draw = 0.26 * math.exp(-((diff / 250.0) ** 2))
    win_a = e_a * (1.0 - draw)
    win_b = (1.0 - e_a) * (1.0 - draw)
    return win_a, draw, win_b


def calc_handicap(elo_a, elo_b, handicap):
    elo_adj = handicap * 80
    p_cover = elo_win_probability(elo_a + elo_adj, elo_b)
    if handicap == int(handicap) and handicap != 0:
        diff = abs(elo_a - elo_b)
        push = 0.12 * math.exp(-((diff / 200.0) ** 2))
        p_cover = p_cover * (1.0 - push)
        return p_cover, 1.0 - p_cover - push, push
    return p_cover, 1.0 - p_cover, 0.0


def poisson_pmf(lmbda, k):
    if lmbda <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lmbda) * (lmbda ** k) / math.factorial(k)


def predict_scores(elo_a, elo_b):
    avg_goals = 1.5
    exp_g_a = avg_goals * math.exp((elo_a - elo_b) / 400.0 * 0.85)
    exp_g_b = avg_goals * math.exp((elo_b - elo_a) / 400.0 * 0.85)
    exp_g_a = max(0.3, min(exp_g_a, 5.0))
    exp_g_b = max(0.3, min(exp_g_b, 5.0))

    scores = []
    for ga in range(0, 7):
        for gb in range(0, 7):
            prob = poisson_pmf(exp_g_a, ga) * poisson_pmf(exp_g_b, gb)
            scores.append((ga, gb, prob))
    scores.sort(key=lambda x: x[2], reverse=True)

    result = []
    for s in scores:
        if len(result) >= 2:
            break
        if s[0] == s[1] and abs(elo_a - elo_b) > 200 and len(result) > 0:
            continue
        result.append(s)
    return result


def predict_total_goals(elo_a, elo_b):
    """Return top 2 total goals predictions from Poisson model."""
    avg_goals = 1.5
    exp_g_a = avg_goals * math.exp((elo_a - elo_b) / 400.0 * 0.85)
    exp_g_b = avg_goals * math.exp((elo_b - elo_a) / 400.0 * 0.85)
    exp_g_a = max(0.3, min(exp_g_a, 5.0))
    exp_g_b = max(0.3, min(exp_g_b, 5.0))
    exp_total = exp_g_a + exp_g_b

    probs = []
    for tg in range(0, 11):
        prob = poisson_pmf(exp_total, tg)
        probs.append((tg, prob))
    probs.sort(key=lambda x: x[1], reverse=True)
    return probs[:2]


def form_score(form_str):
    points = {"W": 3, "w": 3, "D": 1, "d": 1, "L": 0, "l": 0}
    results = form_str.strip().split("-")
    if len(results) != 5:
        return 0.0
    weights = [0.35, 0.25, 0.20, 0.12, 0.08]
    score = sum(points.get(r.strip(), 1) * weights[i] for i, r in enumerate(results))
    return round((score / 3.0) * 10 - 5, 1)


def form_to_display(form_str):
    mapping = {"W": "胜", "D": "平", "L": "负", "w": "胜", "d": "平", "l": "负"}
    return "".join(mapping.get(r.strip(), "?") for r in form_str.strip().split("-"))


def form_summary(form_str):
    results = form_str.strip().split("-")
    w = sum(1 for r in results if r.strip().upper() == "W")
    d = sum(1 for r in results if r.strip().upper() == "D")
    l_count = sum(1 for r in results if r.strip().upper() == "L")
    return f"近5场{w}胜{d}平{l_count}负"


# ---------- live odds ----------


def implied_odds_from_elo(elo1, elo2):
    """Generate ELO-implied fair odds (decimal) when live API unavailable."""
    w1, d, w2 = calc_match_probabilities(elo1, elo2)
    return {
        "source": "ELO隐含",
        "european": {"home": round(1.0 / w1, 2), "draw": round(1.0 / d, 2), "away": round(1.0 / w2, 2)},
        "asian": {"line": None, "home": None, "away": None},
    }


def _round_odds(raw):
    """Round to 0.5 increments (竞彩 format), min 1.20."""
    val = int(raw * 2) / 2
    return max(1.20, val)


def lottery_odds(elo1, elo2, w1, d, w2):
    """
    Estimated Chinese Sports Lottery odds (竞彩赔率).
    Uses ~88% payout rate, min odds 1.20, draw min 2.50.
    """
    payout = 0.88
    home = max(1.20, _round_odds(1.0 / w1 * payout))
    draw = max(2.50, _round_odds(1.0 / d * payout))
    away = max(1.20, _round_odds(1.0 / w2 * payout))
    return {"home": home, "draw": draw, "away": away}


def handicap_lottery_odds(cover_p, not_cover_p):
    """Estimated 竞彩让球赔率 for handicap line."""
    payout = 0.85
    cover = _round_odds(1.0 / cover_p * payout) if cover_p > 0 else 9.99
    not_cover = _round_odds(1.0 / not_cover_p * payout) if not_cover_p > 0 else 9.99
    return {"cover": cover, "not_cover": not_cover}


def fetch_live_odds(team1, team2, elo1, elo2):
    """
    Fetch live odds from the-odds-api.com.
    Falls back to ELO-implied odds if API key not set or fetch fails.
    Returns dict with 'european' and 'asian' odds.
    """
    if not ODDS_API_KEY:
        return implied_odds_from_elo(elo1, elo2)

    # Try both sport keys: soccer_world_cup_winner (outright) + search for match odds
    sport_keys = ["soccer_world_cup", "soccer_fifa_world_cup"]

    for sport in sport_keys:
        try:
            url = (
                f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
                f"?apiKey={ODDS_API_KEY}"
                f"&regions=eu,us"
                f"&markets=h2h,spreads"
                f"&oddsFormat=decimal"
            )
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return _parse_odds_response(data, team1, team2, elo1, elo2)
        except Exception as e:
            print(f"  [WARN] Odds API ({sport}): {e}")
            continue

    return implied_odds_from_elo(elo1, elo2)


def _parse_odds_response(data, team1, team2, elo1, elo2):
    """Parse the-odds-api response, matching teams by name substring."""
    t1_lower = team1.lower()
    t2_lower = team2.lower()

    for event in data:
        home = event.get("home_team", "").lower()
        away = event.get("away_team", "").lower()
        if (t1_lower in home or home in t1_lower) or \
           (t1_lower in away or away in t1_lower):
            bookmakers = event.get("bookmakers", [])
            if not bookmakers:
                continue

            # Average odds across bookmakers
            h2h_odds = []
            spread_odds = []
            for bk in bookmakers:
                for mkt in bk.get("markets", []):
                    if mkt.get("key") == "h2h":
                        for outcome in mkt.get("outcomes", []):
                            h2h_odds.append(outcome)
                    elif mkt.get("key") == "spreads":
                        for outcome in mkt.get("outcomes", []):
                            spread_odds.append(outcome)

            if h2h_odds:
                home_odds = [o["price"] for o in h2h_odds if o["name"] == event.get("home_team")]
                away_odds = [o["price"] for o in h2h_odds if o["name"] == event.get("away_team")]
                draw_odds = [o["price"] for o in h2h_odds if o["name"] == "Draw"]
                return {
                    "source": "live",
                    "european": {
                        "home": round(sum(home_odds) / len(home_odds), 2) if home_odds else None,
                        "draw": round(sum(draw_odds) / len(draw_odds), 2) if draw_odds else None,
                        "away": round(sum(away_odds) / len(away_odds), 2) if away_odds else None,
                    },
                    "asian": _parse_asian_spreads(spread_odds, event),
                }

    return implied_odds_from_elo(elo1, elo2)


def _parse_asian_spreads(outcomes, event):
    """Extract Asian handicap spread closest to 0.5 from bookmaker data."""
    if not outcomes:
        return {"line": None, "home": None, "away": None}

    # Find spread closest to absolute value 0.5
    best = None
    for o in outcomes:
        pt = o.get("point", 0)
        if best is None or abs(abs(pt) - 0.5) < abs(abs(best["point"]) - 0.5):
            best = o

    if best:
        return {
            "line": best.get("point"),
            "home": best.get("price") if best["name"] == event.get("home_team") else None,
            "away": None,
        }
    return {"line": None, "home": None, "away": None}


def odds_summary(odds, elo1, elo2):
    """Build a one-line odds summary string."""
    if odds is None:
        odds = implied_odds_from_elo(elo1, elo2)

    src = odds["source"]
    eu = odds.get("european", {})
    home = eu.get("home")
    draw = eu.get("draw")
    away = eu.get("away")

    if home and draw and away:
        return f"欧盘 {home}/{draw}/{away} ({src})"
    return f"欧盘 暂无 ({src})"


# ---------- analysis pipeline ----------


def analyze_match(match, elo_data, form_data, fetch_odds=False, sp_odds=None):
    """Full analysis for a single match, blending market SP odds with ELO."""
    team1 = match["team1"]
    team2 = match["team2"]

    elo1 = resolve_elo(team1, elo_data)
    elo2 = resolve_elo(team2, elo_data)

    if elo1 is None or elo2 is None:
        return {
            "skip": True,
            "team1": team1,
            "team2": team2,
            "reason": "TBD (knockout placeholder)",
        }

    form1_str = resolve_form(team1, elo_data, form_data)
    form2_str = resolve_form(team2, elo_data, form_data)

    # --- ELO-based probabilities ---
    e_w1, e_d, e_w2 = calc_match_probabilities(elo1, elo2)
    fs1 = form_score(form1_str)
    fs2 = form_score(form2_str)
    adj = (fs1 - fs2) * 0.012
    e_w1 = max(0.02, min(0.98, e_w1 + adj))
    e_w2 = max(0.02, min(0.98, e_w2 - adj))
    e_d = 1.0 - e_w1 - e_w2

    # --- Market SP odds (from sporttery.cn) ---
    sp_had = None
    sp_hhad = None
    if sp_odds:
        t1_cn = resolve_sporttery_team(team1)
        t2_cn = resolve_sporttery_team(team2)
        key = (t1_cn, t2_cn) if t1_cn and t2_cn else None
        if key and key in sp_odds:
            sp_had = sp_odds[key].get("had")
            sp_hhad = sp_odds[key].get("hhad")

    # Blended probability: 70% market, 30% ELO
    if sp_had:
        inv_sum = 1.0 / sp_had["h"] + 1.0 / sp_had["d"] + 1.0 / sp_had["a"]
        m_w1 = (1.0 / sp_had["h"]) / inv_sum
        m_d = (1.0 / sp_had["d"]) / inv_sum
        m_w2 = (1.0 / sp_had["a"]) / inv_sum
        w1 = m_w1 * 0.70 + e_w1 * 0.30
        d = m_d * 0.70 + e_d * 0.30
        w2 = m_w2 * 0.70 + e_w2 * 0.30
        odds_source = "竞彩官方"
    else:
        w1, d, w2 = e_w1, e_d, e_w2
        odds_source = "ELO隐含"

    # --- Handicap: use sporttery HHAD if available ---
    if sp_hhad and sp_hhad["line"] != 0:
        # Use official 竞彩 handicap line
        official_line = sp_hhad["line"]
        cov, nc, push = calc_handicap(elo1, elo2, official_line)
        hcaps = [{"line": official_line, "cover": cov, "not_cover": nc, "push": push}]
        best_hcap = hcaps[0]
        # HHAD SP implied probabilities
        inv_sum_hh = 1.0/sp_hhad["h"] + 1.0/sp_hhad["d"] + 1.0/sp_hhad["a"]
        mh_cover = (1.0/sp_hhad["h"]) / inv_sum_hh
        mh_not = (1.0/sp_hhad["a"]) / inv_sum_hh
        if cov > 0 and mh_cover > 0:
            cov = cov * 0.5 + mh_cover * 0.5
            nc = 1.0 - cov - push if push > 0 else 1.0 - cov
            hcaps[0] = {"line": official_line, "cover": cov, "not_cover": nc, "push": push}
            best_hcap = hcaps[0]
    else:
        # Fallback: ELO-based handicap
        if elo1 >= elo2:
            handicap_lines = [-1, -2, -3]
        else:
            handicap_lines = [1, 2, 3]
        hcaps = []
        for hlin in handicap_lines:
            cov, nc, push = calc_handicap(elo1, elo2, hlin)
            hcaps.append({"line": hlin, "cover": cov, "not_cover": nc, "push": push})
        valid = [h for h in hcaps if h["cover"] > 0.30]
        if valid:
            best_hcap = min(valid, key=lambda h: abs(h["cover"] - 0.5))
        else:
            best_hcap = hcaps[0]

    # Scores & total goals
    scores = predict_scores(elo1, elo2)
    total_goals = predict_total_goals(elo1, elo2)

    # Favorite
    if w1 > w2 + 0.05:
        favorite = team1
        fav_pct = w1
    elif w2 > w1 + 0.05:
        favorite = team2
        fav_pct = w2
    else:
        favorite = None

    # Live odds from the-odds-api
    odds = None
    if fetch_odds:
        odds = fetch_live_odds(team1, team2, elo1, elo2)

    # Lottery odds — use sporttery SP if available, else ELO estimate
    if sp_had:
        lotto = {"home": sp_had["h"], "draw": sp_had["d"], "away": sp_had["a"]}
    else:
        lotto = lottery_odds(elo1, elo2, w1, d, w2)

    if sp_hhad:
        inv_hh = 1.0/sp_hhad["h"] + 1.0/sp_hhad["d"] + 1.0/sp_hhad["a"]
        best_hcap_lotto = {
            "cover": round(1.0/(1.0/sp_hhad["h"]/inv_hh) * 0.88, 2),
            "not_cover": round(1.0/(1.0/sp_hhad["a"]/inv_hh) * 0.88, 2)
        }
    else:
        best_hcap_lotto = handicap_lottery_odds(best_hcap["cover"], best_hcap["not_cover"])

    return {
        "skip": False,
        "team1": team1, "team2": team2,
        "elo1": elo1, "elo2": elo2,
        "group": match.get("group", ""),
        "date": match.get("date", ""),
        "time": match.get("time", ""),
        "ground": match.get("ground", ""),
        "win1": w1, "draw": d, "win2": w2,
        "handicaps": hcaps,
        "best_handicap": best_hcap,
        "scores": scores,
        "total_goals": total_goals,
        "form1": form1_str, "form2": form2_str,
        "form1_score": fs1, "form2_score": fs2,
        "favorite": favorite,
        "fav_pct": fav_pct if favorite else 0,
        "odds": odds,
        "lotto": lotto,
        "hcap_lotto": best_hcap_lotto,
        "odds_source": odds_source,
        "sp_had": sp_had,
        "sp_hhad": sp_hhad,
    }



# ---------- WeChat formatting ----------

FLAGS = {
    "Mexico": "🇲🇽", "South Africa": "🇿🇦", "South Korea": "🇰🇷",
    "Czech Republic": "🇨🇿", "Canada": "🇨🇦", "Bosnia & Herzegovina": "🇧🇦",
    "Qatar": "🇶🇦", "Switzerland": "🇨🇭", "Brazil": "🇧🇷", "Morocco": "🇲🇦",
    "Haiti": "🇭🇹", "Scotland": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "USA": "🇺🇸", "United States": "🇺🇸",
    "Turkey": "🇹🇷", "Australia": "🇦🇺", "Paraguay": "🇵🇾", "Germany": "🇩🇪",
    "Ecuador": "🇪🇨", "Ivory Coast": "🇨🇮", "Curacao": "🇨🇼",
    "Curaçao": "🇨🇼", "Netherlands": "🇳🇱", "Japan": "🇯🇵",
    "Sweden": "🇸🇪", "Tunisia": "🇹🇳", "Belgium": "🇧🇪", "Iran": "🇮🇷",
    "Egypt": "🇪🇬", "New Zealand": "🇳🇿", "Spain": "🇪🇸",
    "Uruguay": "🇺🇾", "Cape Verde": "🇨🇻", "Saudi Arabia": "🇸🇦",
    "France": "🇫🇷", "Norway": "🇳🇴", "Senegal": "🇸🇳", "Iraq": "🇮🇶",
    "Argentina": "🇦🇷", "Austria": "🇦🇹", "Jordan": "🇯🇴", "Algeria": "🇩🇿",
    "Portugal": "🇵🇹", "Colombia": "🇨🇴", "Uzbekistan": "🇺🇿",
    "DR Congo": "🇨🇩", "England": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "Croatia": "🇭🇷",
    "Panama": "🇵🇦", "Ghana": "🇬🇭",
}

CN_NAMES = {
    "Mexico": "墨西哥", "South Africa": "南非", "South Korea": "韩国",
    "Czech Republic": "捷克", "Canada": "加拿大", "Bosnia & Herzegovina": "波黑",
    "Qatar": "卡塔尔", "Switzerland": "瑞士", "Brazil": "巴西", "Morocco": "摩洛哥",
    "Haiti": "海地", "Scotland": "苏格兰", "USA": "美国", "United States": "美国",
    "Turkey": "土耳其", "Australia": "澳大利亚", "Paraguay": "巴拉圭",
    "Germany": "德国", "Ecuador": "厄瓜多尔", "Ivory Coast": "科特迪瓦",
    "Curacao": "库拉索", "Curaçao": "库拉索", "Netherlands": "荷兰", "Japan": "日本",
    "Sweden": "瑞典", "Tunisia": "突尼斯", "Belgium": "比利时", "Iran": "伊朗",
    "Egypt": "埃及", "New Zealand": "新西兰", "Spain": "西班牙",
    "Uruguay": "乌拉圭", "Cape Verde": "佛得角", "Saudi Arabia": "沙特阿拉伯",
    "France": "法国", "Norway": "挪威", "Senegal": "塞内加尔", "Iraq": "伊拉克",
    "Argentina": "阿根廷", "Austria": "奥地利", "Jordan": "约旦", "Algeria": "阿尔及利亚",
    "Portugal": "葡萄牙", "Colombia": "哥伦比亚", "Uzbekistan": "乌兹别克斯坦",
    "DR Congo": "刚果(金)", "England": "英格兰", "Croatia": "克罗地亚",
    "Panama": "巴拿马", "Ghana": "加纳",
}

CN_GROUPS = {
    "Group A": "A组", "Group B": "B组", "Group C": "C组", "Group D": "D组",
    "Group E": "E组", "Group F": "F组", "Group G": "G组", "Group H": "H组",
    "Group I": "I组", "Group J": "J组", "Group K": "K组", "Group L": "L组",
}

CN_VENUES = {
    "Mexico City": "墨西哥城",
    "Guadalajara (Zapopan)": "瓜达拉哈拉",
    "Monterrey (Guadalupe)": "蒙特雷",
    "Toronto": "多伦多",
    "Vancouver": "温哥华",
    "San Francisco Bay Area (Santa Clara)": "旧金山湾区",
    "Los Angeles (Inglewood)": "洛杉矶",
    "Seattle": "西雅图",
    "New York/New Jersey (East Rutherford)": "纽约/新泽西",
    "Boston (Foxborough)": "波士顿",
    "Philadelphia": "费城",
    "Atlanta": "亚特兰大",
    "Miami (Miami Gardens)": "迈阿密",
    "Houston": "休斯顿",
    "Dallas (Arlington)": "达拉斯",
    "Kansas City": "堪萨斯城",
}

ROUND_CN = {
    "Matchday 1": "小组赛第1轮", "Matchday 2": "小组赛第2轮",
    "Matchday 3": "小组赛第3轮", "Matchday 8": "小组赛第2轮",
    "Matchday 14": "小组赛第3轮",
    "Round of 32": "1/16决赛", "Round of 16": "1/8决赛",
    "Quarter-finals": "1/4决赛", "Quarter-final": "1/4决赛",
    "Semi-finals": "半决赛", "Semi-final": "半决赛",
    "Match for third place": "三四名决赛", "Final": "决赛",
}


def flag(team_name):
    return FLAGS.get(team_name, "")


def cn(team_name):
    return CN_NAMES.get(team_name, team_name)


def cn_group(group_en):
    return CN_GROUPS.get(group_en, group_en)


def cn_venue(venue_en):
    for k, v in CN_VENUES.items():
        if k in venue_en:
            return v
    return venue_en


# ---------- image rendering ----------

def _find_chinese_font():
    """Find a Chinese-capable font on the system."""
    # Search by font properties (CJK coverage)
    for f in font_manager.fontManager.ttflist:
        try:
            for keyword in ("Microsoft YaHei", "SimHei", "WenQuanYi",
                             "Noto Sans CJK", "NotoSansCJK", "Droid Sans Fallback",
                             "PingFang", "Arial Unicode", "WenQuanYi Zen Hei"):
                if keyword.lower() in f.name.lower():
                    return f.name
        except Exception:
            continue
    # Fallback: try to rebuild font list scanning system dirs
    for f in font_manager.fontManager.ttflist:
        fname_lower = f.fname.lower()
        if any(k in fname_lower for k in ("msyh", "simhei", "wqy", "noto", "cjki", "droid")):
            return f.name
    return font_manager.fontManager.ttflist[0].name


def render_prediction_image(results, match_date_str):
    """
    Render match predictions as a clean table image, one per match.
    Returns a list of (team_label, image_base64, image_md5) tuples.
    """
    font_name = _find_chinese_font()
    analyzed = [r for r in results if not r.get("skip")]

    images = []
    for r in analyzed:
        t1, t2 = r["team1"], r["team2"]
        c1, c2 = cn(t1), cn(t2)
        group = cn_group(r.get("group", ""))
        venue = cn_venue(r.get("ground", ""))
        bj_time, next_day = to_beijing_time(r.get("time", ""))
        time_display = f"{bj_time} 北京时间"
        if next_day:
            time_display += "(次日)"

        w1 = r["win1"] * 100
        d = r["draw"] * 100
        w2 = r["win2"] * 100
        bh = r["best_handicap"]

        odds = r.get("odds")
        eu = odds.get("european", {}) if odds else {}

        fd1 = form_to_display(r["form1"])
        fd2 = form_to_display(r["form2"])
        fs1 = form_summary(r["form1"])
        fs2 = form_summary(r["form2"])

        # Build table data
        rows_data = [
            # (section, rows...)
            ("[ DATA 数据 ]", [
                f"竞彩SP: {r.get('odds_source', '')} 胜{r['lotto']['home']} / 平{r['lotto']['draw']} / 负{r['lotto']['away']}",
                f"状态: {c1} {fs1}({fd1})",
                f"      {c2} {fs2}({fd2})",
                f"ELO:  {c1} {r['elo1']} vs {c2} {r['elo2']} (差{r['elo1']-r['elo2']:+d})",
            ]),
            ("[ PREDICT 预测 ]", [
                f"{c1}胜 {w1:.0f}%   平 {d:.0f}%   {c2}胜 {w2:.0f}%",
                _hcap_text(c1, bh, r.get("hcap_lotto")),
                _score_text(r["scores"]),
                _total_text(r["total_goals"]),
            ]),
        ]

        # Render
        label = f"{flag(t1)}{c1}vs{flag(t2)}{c2}"
        img_b64, img_md5 = _render_single_image(
            match_date_str, c1, c2, group, time_display, venue, rows_data, font_name
        )
        images.append((label, img_b64, img_md5))

    return images


def _hcap_text(c1, bh, hcap_lotto=None):
    hcap_line = f"{c1}{bh['line']:+d}"
    base = f"竞彩让球 {hcap_line}: 赢盘 {bh['cover']*100:.0f}% / 输盘 {bh['not_cover']*100:.0f}%"
    if hcap_lotto:
        base += f" (赔 {hcap_lotto['cover']}/{hcap_lotto['not_cover']})"
    return base


def _score_text(scores):
    parts = [f"{g[0]}-{g[1]} ({g[2]*100:.1f}%)" for g in scores]
    return "比分: " + " / ".join(parts)


def _total_text(total_goals):
    parts = [f"{tg}球 ({prob*100:.1f}%)" for tg, prob in total_goals]
    return "总进球: " + " / ".join(parts)


def _render_single_image(date_str, c1, c2, group, time_disp, venue, rows_data, font_name):
    """Render one match as a vertical PNG for mobile viewing."""
    fig, ax = plt.subplots(figsize=(6, 8))
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    fig.patch.set_facecolor("#1a1a2e")

    x_margin = 0.08
    right_edge = 0.92

    # ---- Header: compact title + fixture ----
    y = 0.97
    ax.text(0.5, y, f"World Cup 2026 世界杯预测 · {date_str}",
            fontsize=12, color="#e94560",
            fontfamily=font_name, transform=ax.transAxes, va="top", ha="center")
    y -= 0.045
    ax.text(0.5, y, f"{c1}  vs  {c2}",
            fontsize=16, fontweight="bold", color="#ffffff",
            fontfamily=font_name, transform=ax.transAxes, va="top", ha="center")
    y -= 0.05
    ax.text(0.5, y, f"{group}  |  {time_disp}  |  {venue}",
            fontsize=10, color="#aaaaaa",
            fontfamily=font_name, transform=ax.transAxes, va="top", ha="center")
    y -= 0.06

    # ---- Separator ----
    ax.plot([x_margin, right_edge], [y, y],
            color="#444477", linewidth=0.5, transform=ax.transAxes, clip_on=False)
    y -= 0.04

    # ---- Data section (compact) ----
    ax.text(x_margin, y, "DATA  数据",
            fontsize=12, fontweight="bold", color="#f0c040",
            fontfamily=font_name, transform=ax.transAxes, va="top")
    y -= 0.055

    for row in rows_data[0][1]:
        ax.text(0.5, y, row,
                fontsize=10.5, color="#d0d0e0",
                fontfamily=font_name, transform=ax.transAxes, va="top", ha="center")
        y -= 0.05
    y -= 0.03

    # ---- Prediction section (main, large) ----
    ax.plot([x_margin, right_edge], [y, y],
            color="#444477", linewidth=0.5, transform=ax.transAxes, clip_on=False)
    y -= 0.04
    ax.text(x_margin, y, "PREDICT  预测",
            fontsize=14, fontweight="bold", color="#f0c040",
            fontfamily=font_name, transform=ax.transAxes, va="top")
    y -= 0.08

    for row in rows_data[1][1]:
        ax.text(0.5, y, row,
                fontsize=12.5, color="#d0d0e0",
                fontfamily=font_name, transform=ax.transAxes, va="top", ha="center")
        y -= 0.075
    y -= 0.02

    # Save
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor="#1a1a2e", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    img_bytes = buf.getvalue()

    img_b64 = base64.b64encode(img_bytes).decode("ascii")
    img_md5 = hashlib.md5(img_bytes).hexdigest()
    return img_b64, img_md5


# ---------- parlay image ----------


def render_parlay_image(parlay_text, match_date_str, font_name):
    """Render the parlay text as a styled image with dark green background."""
    if not parlay_text:
        return None, None

    fig, ax = plt.subplots(figsize=(6, 8))
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    fig.patch.set_facecolor("#0f2b24")  # dark teal/green

    y = 0.96
    # Title
    ax.text(0.5, y, f"World Cup 2026 串关精选",
            fontsize=18, fontweight="bold", color="#70e0a0",
            fontfamily=font_name, transform=ax.transAxes, va="top", ha="center")
    y -= 0.05
    ax.text(0.5, y, f"{match_date_str} 比赛日",
            fontsize=14, color="#50c878",
            fontfamily=font_name, transform=ax.transAxes, va="top", ha="center")
    y -= 0.08

    # Parse parlay text to extract combos
    # Format: "### 🥇 3串1 (胜率 `23%` / 总赔率 `2.43`)\n> **A** × **B**\n明细: ..."
    sections = parlay_text.split("### ")
    tags = ["[1st]", "[2nd]", "[3rd]"]

    for i, section in enumerate(sections):
        if not section.strip() or "串关精选" in section:
            continue
        lines_in = section.strip().split("\n")

        # Header line: "🥇 3串1 (胜率 23% / 总赔率 2.43)"
        header = lines_in[0].strip()
        # Clean markdown formatting
        header = header.replace("`", "")
        ax.text(0.08, y, header,
                fontsize=14, fontweight="bold", color="#f0e060",
                fontfamily=font_name, transform=ax.transAxes, va="top")
        y -= 0.065

        # Combo line: "> **pick1** × **pick2** × **pick3**"
        if len(lines_in) > 1:
            combo = lines_in[1].replace("> ", "").replace("**", "").strip()
            ax.text(0.5, y, combo,
                    fontsize=12, color="#ffffff",
                    fontfamily=font_name, transform=ax.transAxes, va="top", ha="center")
            y -= 0.055

        # Detail line: "明细: A [胜负] 68% @1.5 | B [让球] 65% @1.5"
        if len(lines_in) > 2:
            detail = lines_in[2].replace("明细: ", "").replace("`", "").strip()
            parts = detail.split(" | ")
            for part in parts:
                ax.text(0.12, y, part.strip(),
                        fontsize=9.5, color="#b0d0c0",
                        fontfamily=font_name, transform=ax.transAxes, va="top")
                y -= 0.04
        y -= 0.03

    # Footer disclaimer
    ax.text(0.5, 0.04, "算法生成 · 竞彩参考 · 理性投注",
            fontsize=9, color="#507060", fontfamily=font_name,
            transform=ax.transAxes, va="center", ha="center")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor="#0f2b24", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    img_bytes = buf.getvalue()
    return base64.b64encode(img_bytes).decode("ascii"), hashlib.md5(img_bytes).hexdigest()


# ---------- format_wechat (fallback markdown) ----------


def format_wechat(results, match_date_str):
    lines = [
        f"# ⚽ 世界杯预测 | {match_date_str}比赛日",
        "",
    ]

    skipped = [r for r in results if r.get("skip")]
    analyzed = [r for r in results if not r.get("skip")]

    for r in analyzed:
        t1, t2 = r["team1"], r["team2"]
        c1, c2 = cn(t1), cn(t2)
        group = cn_group(r.get("group", ""))
        venue = cn_venue(r.get("ground", ""))

        # Beijing time
        bj_time, next_day = to_beijing_time(r.get("time", ""))
        time_display = f"{bj_time} 北京时间"
        if next_day:
            time_display += "(次日)"

        w1 = r["win1"] * 100
        d = r["draw"] * 100
        w2 = r["win2"] * 100

        # Build match header
        lines.append(f"## {flag(t1)} {c1} vs {flag(t2)} {c2}")
        lines.append(f"> {group} | {time_display} | {venue}")
        lines.append("")

        # === DATA SECTION ===
        lines.append("**📊 数据**")

        # European odds
        odds = r.get("odds")
        if odds:
            eu = odds.get("european", {})
            h_odd = eu.get("home")
            d_odd = eu.get("draw")
            a_odd = eu.get("away")
            odd_src = odds.get("source", "ELO隐含")
            if h_odd and d_odd and a_odd:
                lines.append(f"- 💰 欧盘: 胜{h_odd} / 平{d_odd} / 负{a_odd} ({odd_src})")
            else:
                lines.append(f"- 💰 欧盘: 暂无 ({odd_src})")
        else:
            lines.append(f"- 💰 欧盘: ELO隐含 胜{1/w1*100:.0f}%/{1/d*100:.0f}%/{1/w2*100:.0f}%")

        # Form
        fd1 = form_to_display(r["form1"])
        fd2 = form_to_display(r["form2"])
        fs1 = form_summary(r["form1"])
        fs2 = form_summary(r["form2"])
        lines.append(f"- 💡 状态: {c1} {fs1}({fd1}) | {c2} {fs2}({fd2})")

        # ELO
        lines.append(f"- 📈 ELO: {c1} {r['elo1']} vs {c2} {r['elo2']} (差{r['elo1']-r['elo2']:+d})")

        lines.append("")

        # === PREDICTION SECTION ===
        lines.append("**🔮 预测**")

        # Win/Draw/Loss
        lines.append(f"> 胜负: {c1}胜 `{w1:.0f}%` / 平 `{d:.0f}%` / {c2}胜 `{w2:.0f}%`")

        # Handicap
        bh = r["best_handicap"]
        hcap_line = f"{c1}{bh['line']:+.1f}"
        if bh["push"] > 0.01:
            lines.append(
                f"> 让球: {hcap_line} 赢盘 `{bh['cover']*100:.0f}%` "
                f"/ 走水 `{bh['push']*100:.0f}%` "
                f"/ 输盘 `{bh['not_cover']*100:.0f}%`"
            )
        else:
            lines.append(
                f"> 让球: {hcap_line} 赢盘 `{bh['cover']*100:.0f}%` "
                f"/ 输盘 `{bh['not_cover']*100:.0f}%`"
            )

        # Scores
        score_strs = []
        for ga, gb, prob in r["scores"]:
            score_strs.append(f"`{ga}-{gb}` ({prob*100:.1f}%)")
        lines.append(f"> 比分: {' / '.join(score_strs)}")

        lines.append("")

    if skipped:
        lines.append("---")
        lines.append("### ⏳ 待定场次")
        for s in skipped:
            lines.append(
                f"- {flag(s['team1'])} {cn(s['team1'])} vs "
                f"{flag(s['team2'])} {cn(s['team2'])}: {s['reason']}"
            )
        lines.append("")

    lines.append("---")
    lines.append("> 🤖 模型: ELO + Poisson + 近期状态加权")
    lines.append("> 💰 赔率: ELO隐含赔率 (配置ODDS_API_KEY获取实时赔率)")
    lines.append("> ⚠ 仅供参考，不构成投注建议")

    return "\n".join(lines)


# ---------- WeChat send ----------


def send_wechat_images(images, results, match_date_str):
    """Push prediction images to WeChat Work bot, one per match, + parlay text."""
    print(f"\n📤 推送 {len(images)} 张预测图到企业微信...")

    # Header message
    header = {
        "msgtype": "markdown",
        "markdown": {"content": f"# ⚽ 世界杯预测 | {match_date_str}比赛日\n共{len(images)}场比赛 👇"},
    }
    try:
        r = requests.post(WEBHOOK_URL, json=header, timeout=15)
        print(f"  [header] {r.json()}")
    except Exception as e:
        print(f"  [WARN] header: {e}")

    for label, img_b64, img_md5 in images:
        payload = {
            "msgtype": "image",
            "image": {"base64": img_b64, "md5": img_md5},
        }
        try:
            r = requests.post(WEBHOOK_URL, json=payload, timeout=20)
            result = r.json()
            if result.get("errcode") == 0:
                print(f"  [OK] {label}")
            else:
                print(f"  [WARN] {label}: {result}")
        except Exception as e:
            print(f"  [ERROR] {label}: {e}")

    # Footer text
    footer = {
        "msgtype": "markdown",
        "markdown": {"content": "> ⚠ 数据来源 ELO + Poisson | 竞彩赔率为模拟值 | 仅供参考"},
    }
    try:
        requests.post(WEBHOOK_URL, json=footer, timeout=15)
    except Exception:
        pass

    # Parlay: first 4 matches within 24h window
    analyzed = [r for r in results if not r.get("skip")]
    if analyzed:
        first_time = analyzed[0].get("time", "")
        parlay_results = analyzed[:4]  # max 4 matches
    else:
        parlay_results = []

    parlay = generate_parlay(parlay_results, match_date_str)
    if parlay:
        p_b64, p_md5 = render_parlay_image(parlay, match_date_str,
                                            _find_chinese_font())
        if p_b64:
            payload = {"msgtype": "image", "image": {"base64": p_b64, "md5": p_md5}}
            try:
                r = requests.post(WEBHOOK_URL, json=payload, timeout=20)
                print(f"  [parlay] {r.json()}")
            except Exception as e:
                print(f"  [WARN] parlay image: {e}")


# ---------- M串N definitions ----------

MSN_METHODS = {
    2: {"2串1": [(2, 1)]},  # 1 slip of 2-match
    3: {
        "3串1": [(3, 1)],
        "3串3": [(2, 3)],
        "3串4": [(2, 3), (3, 1)],
    },
    4: {
        "4串1": [(4, 1)],
        "4串4": [(3, 4)],
        "4串5": [(3, 4), (4, 1)],
        "4串6": [(2, 6)],
        "4串11": [(2, 6), (3, 4), (4, 1)],
    },
}


def _msn_calc(picks_data, method_name, m):
    """
    Calculate M串N parlay: cost, max_return, and expected return.
    picks_data: list of (label, win_prob, type, sp_odds)
    Returns dict with method details.
    """
    methods = MSN_METHODS.get(m, {})
    if method_name not in methods:
        return None

    slip_defs = methods[method_name]
    total_slips = sum(s[1] for s in slip_defs)

    # Each slip costs 2 yuan
    cost = total_slips * 2

    # Calculate payout for each possible outcome
    from itertools import combinations as combos_slip

    max_return = 0.0
    expected_return = 0.0

    # For each slip definition (combo_size, count)
    for combo_size, count in slip_defs:
        for indices in combos_slip(range(m), combo_size):
            slip_odds = 1.0
            slip_prob = 1.0
            for idx in indices:
                slip_odds *= picks_data[idx][3]  # SP odds
                slip_prob *= picks_data[idx][1]   # win probability
            slip_payout = 2 * slip_odds
            max_return += slip_payout
            expected_return += slip_payout * slip_prob

    return {
        "name": method_name,
        "slips": total_slips,
        "cost": cost,
        "max_return": round(max_return, 2),
        "expected_return": round(expected_return, 2),
        "ev_ratio": round(expected_return / cost, 2) if cost > 0 else 0,
    }


def generate_parlay(results, match_date_str):
    """
    Top 3 combinations from ANY subset of 2-4 matches, ALL M串N methods.
    Ranked by EV ratio (性价比).
    """
    analyzed = [r for r in results if not r.get("skip")]
    if len(analyzed) < 2:
        return None
    if len(analyzed) > 4:
        analyzed = analyzed[:4]  # max 4

    # Build all picks per match — only include bet types that sporttery.cn has opened
    all_picks = []
    for r in analyzed:
        c1, c2 = cn(r["team1"]), cn(r["team2"])
        w1, w2 = r["win1"], r["win2"]
        lotto = r["lotto"]
        sp_had = r.get("sp_had")
        sp_hhad = r.get("sp_hhad")
        match_name = f"{c1}vs{c2}"
        options = []
        # Win/Loss pick — only if HAD is open on sporttery
        if sp_had:
            if w1 >= w2:
                options.append((f"{c1}胜", w1, "胜负", sp_had["h"], match_name))
            else:
                options.append((f"{c2}胜", w2, "胜负", sp_had["a"], match_name))
        # Handicap pick — only if HHAD is open on sporttery
        if sp_hhad:
            hline = sp_hhad["line"]
            if hline > 0:
                options.append((f"{c2}+{hline}赢盘", r["best_handicap"]["cover"],
                               "让球", sp_hhad["a"], match_name))
            elif hline < 0:
                options.append((f"{c1}{hline}赢盘", r["best_handicap"]["cover"],
                               "让球", sp_hhad["h"], match_name))
        # Fallback: if neither open, use ELO-based pick
        if not sp_had and not sp_hhad:
            if w1 >= w2:
                options.append((f"{c1}胜", w1, "ELO", lotto["home"], match_name))
            else:
                options.append((f"{c2}胜", w2, "ELO", lotto["away"], match_name))
        all_picks.append(options)

    # Enumerate all subsets (size 2, 3, 4) × pick combinations × M串N methods
    from itertools import combinations as icombo

    all_results = []
    for subset_size in range(2, len(all_picks) + 1):
        for subset_indices in icombo(range(len(all_picks)), subset_size):
            _eval_subset(all_picks, list(subset_indices), 0, [], all_results)

    if not all_results:
        return None

    all_results.sort(key=lambda x: x["ev_ratio"], reverse=True)

    # Pick top 3, prefer diversity in size
    seen_sizes = set()
    top = []
    for r in all_results:
        if len(top) >= 3:
            break
        size = r["match_count"]
        if size not in seen_sizes or len(top) >= 1:
            top.append(r)
            seen_sizes.add(size)

    # Format
    labels = {2: "2串组合", 3: "3串组合", 4: "4串组合"}
    lines = [f"# 🎯 竞彩串关 | {match_date_str}", ""]

    for i, r in enumerate(top):
        tags = ["🥇", "🥈", "🥉"]
        combo_str = " × ".join(f"**{p[0]}**" for p in r["picks"])
        match_names = " / ".join(sorted(set(p[4] for p in r["picks"])))
        lines.append(
            f"### {tags[i]} {r['name']} ({r['match_count']}场)"
        )
        lines.append(f"> {combo_str}")
        lines.append(f"> 场次: {match_names}")
        lines.append(
            f"> 成本 `{r['cost']}元` · "
            f"最高返 `{r['max_return']}元` · "
            f"EV比 `{r['ev_ratio']}`"
        )
        lines.append("")

    lines.append("---")
    lines.append("> ⚠ 竞彩官方M串N规则 · 理论最高返奖 · 理性投注")
    return "\n".join(lines)


def _eval_subset(all_picks, indices, depth, current, results):
    """Recurse over picks for a match subset, then evaluate all M串N methods."""
    if depth == len(indices):
        m = len(current)
        methods = MSN_METHODS.get(m, {})
        for method_name in methods:
            r = _msn_calc(current, method_name, m)
            if r:
                r["picks"] = list(current)
                r["match_count"] = m
                results.append(r)
        return

    for pick in all_picks[indices[depth]]:
        _eval_subset(all_picks, indices, depth + 1, current + [pick], results)


def send_wechat_markdown(content):
    """Push markdown to WeChat (fallback), auto-split on 4096 byte limit."""
    max_bytes = 3900
    if len(content.encode("utf-8")) <= max_bytes:
        return _do_send_md(content)

    sections = content.split("\n## ")
    for i, sec in enumerate(sections):
        chunk = sec if i == 0 else "## " + sec
        tag = f" ({i + 1}/{len(sections)})" if len(sections) > 1 else ""
        _do_send_md(chunk + tag)


def _do_send_md(content):
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        result = r.json()
        if result.get("errcode") != 0:
            print(f"  [WARN] WeChat API: {result}")
            return False
        print("  [OK] Sent to WeChat")
        return True
    except Exception as e:
        print(f"  [ERROR] WeChat send failed: {e}")
        return False


# ---------- PushPlus ----------


def format_pushplus(results, parlay_text, match_date_str):
    """Build compact markdown for PushPlus green WeChat push."""
    analyzed = [r for r in results if not r.get("skip")]
    lines = [
        f"# ⚽ 世界杯预测 | {match_date_str}比赛日",
        f"共{len(analyzed)}场比赛",
        "",
    ]
    for r in analyzed:
        c1, c2 = cn(r["team1"]), cn(r["team2"])
        w1, d, w2 = r["win1"]*100, r["draw"]*100, r["win2"]*100
        bj, nd = to_beijing_time(r.get("time", ""))
        lines.append(f"## {flag(r['team1'])} {c1} vs {flag(r['team2'])} {c2}")
        lines.append(f"> {cn_group(r.get('group',''))} | {bj}北京{' 次日' if nd else ''} | {cn_venue(r.get('ground',''))}")
        lines.append("")
        # Data
        lotto = r.get("lotto", {})
        lines.append(f"- 竞彩: 胜{lotto.get('home','-')} / 平{lotto.get('draw','-')} / 负{lotto.get('away','-')}")
        fd1, fd2 = form_to_display(r["form1"]), form_to_display(r["form2"])
        lines.append(f"- 状态: {c1} {form_summary(r['form1'])} {fd1} | {c2} {form_summary(r['form2'])} {fd2}")
        lines.append(f"- ELO: {c1} {r['elo1']} vs {c2} {r['elo2']} (差{r['elo1']-r['elo2']:+d})")
        # Predict
        lines.append(f"- 胜负: {c1} {w1:.0f}% / 平 {d:.0f}% / {c2} {w2:.0f}%")
        bh = r["best_handicap"]
        lines.append(f"- 竞彩让球 {c1}{bh['line']:+d}: 赢盘 {bh['cover']*100:.0f}% / 输盘 {bh['not_cover']*100:.0f}%")
        sg = r["scores"]
        lines.append(f"- 比分: {' / '.join(f'{g[0]}-{g[1]} {g[2]*100:.1f}%' for g in sg)}")
        tg = r["total_goals"]
        lines.append(f"- 总进球: {' / '.join(f'{g[0]}球 {g[1]*100:.1f}%' for g in tg)}")
        lines.append("")

    if parlay_text:
        # Extract just the combo names
        plines = parlay_text.split("\n")
        lines.append(f"## 🎯 串关精选")
        in_combo = False
        for pl in plines:
            if pl.startswith("### "):
                combo_name = pl.replace("### ", "").replace("`", "").strip()
                lines.append(f"- {combo_name}")
            elif pl.startswith("> "):
                lines.append(f"  {pl.replace('> **', '').replace('** ×', ' ×').replace('**', '').strip()}")
        lines.append("")

    lines.append("> ⚠ 算法生成 · 竞彩赔率为模拟值 · 仅供参考")
    return "\n".join(lines)


def send_pushplus(content, match_date_str):
    """Push to PushPlus (green WeChat)."""
    if not PUSHPLUS_TOKEN:
        return
    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": f"⚽ 世界杯预测 · {match_date_str}",
        "content": content,
        "template": "markdown",
    }
    if PUSHPLUS_TOPIC:
        payload["topic"] = PUSHPLUS_TOPIC
    try:
        r = requests.post("https://www.pushplus.plus/send", json=payload, timeout=15)
        result = r.json()
        if result.get("code") == 200:
            print("  [pushplus] OK")
            return True
        else:
            print(f"  [pushplus] {result}")
            return False
    except Exception as e:
        print(f"  [pushplus] ERROR: {e}")
        return False


# ---------- main ----------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="World Cup 2026 Prediction Engine")
    parser.add_argument("--date", help="Target date YYYY-MM-DD (default: today CST)")
    parser.add_argument("--dry-run", action="store_true", help="Print only, no WeChat push")
    parser.add_argument("--no-odds", action="store_true", help="Skip odds fetch (faster)")
    args = parser.parse_args()

    print("=" * 56)
    print("  ⚽ World Cup 2026 Prediction Engine v2")
    print("=" * 56)

    all_matches, elo_data, form_data = load_data()
    print(f"[数据] {len(all_matches)}场比赛, "
          f"{len(elo_data['teams'])}队ELO")

    # Fetch live 竞彩 SP odds from sporttery.cn
    print(f"[竞彩] 获取官方SP赔率...")
    sp_odds = fetch_sporttery_odds()
    print(f"[竞彩] 获取到 {len(sp_odds)} 场赔率数据")

    # Determine target matches
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        window_start = datetime(target_date.year, target_date.month, target_date.day,
                                21, 0, 0, tzinfo=CST)
        window_end = window_start + timedelta(hours=24)
        target_matches = []
        for m in all_matches:
            bj_time, _ = to_beijing_time(m.get("time", ""))
            hh, mm = map(int, bj_time.split(":"))
            md = datetime.strptime(m["date"], "%Y-%m-%d").date()
            mdt = datetime(md.year, md.month, md.day, hh, mm, tzinfo=CST)
            if window_start <= mdt < window_end:
                target_matches.append(m)
        if window_start.date() == window_end.date():
            target_str = window_start.strftime("%m月%d日")
        else:
            target_str = (f"{window_start.strftime('%m月%d日')}~"
                          f"{window_end.strftime('%m月%d日')}")
    else:
        target_matches, target_str = get_matches_in_window(all_matches)

    if not target_matches:
        print(f"\n[信息] {target_str} 无比赛安排，跳过")
        return

    print(f"\n[赛程] {target_str} 共 {len(target_matches)} 场比赛 (21:00→21:00窗口)")

    fetch_odds = not args.no_odds
    results = []
    for m in target_matches:
        t1, t2 = m["team1"], m["team2"]
        c1, c2 = cn(t1), cn(t2)
        bj_time, nd = to_beijing_time(m.get("time", ""))
        print(f"\n[分析] {c1} vs {c2} ({bj_time} 北京)")
        result = analyze_match(m, elo_data, form_data, fetch_odds=fetch_odds,
                               sp_odds=sp_odds)
        results.append(result)
        if result.get("skip"):
            print(f"  ⏭ {result['reason']}")
        else:
            print(f"  ELO: {result['elo1']} vs {result['elo2']}")
            print(f"  胜负: {result['win1']*100:.0f}% / "
                  f"{result['draw']*100:.0f}% / {result['win2']*100:.0f}%")
            score_strs = [f"{g[0]}-{g[1]}" for g in result["scores"]]
            print(f"  比分: {' / '.join(score_strs)}")
            if result.get("odds"):
                print(f"  赔率: {odds_summary(result['odds'], result['elo1'], result['elo2'])}")

    # Render images
    images = render_prediction_image(results, target_str)
    print(f"\n[图片] 生成 {len(images)} 张预测图")

    if args.dry_run:
        print("\n🔇 dry-run模式，跳过推送")
        return

    send_wechat_images(images, results, target_str)
    print("✅ 企业微信推送完成")

    # Also push to PushPlus (green WeChat)
    if PUSHPLUS_TOKEN:
        pp_content = format_pushplus(results,
                                     generate_parlay([r for r in results if not r.get("skip")], target_str),
                                     target_str)
        print("\n📤 推送 PushPlus (绿微信)...")
        send_pushplus(pp_content, target_str)
    print("✅ 全部推送完成")


if __name__ == "__main__":
    main()
