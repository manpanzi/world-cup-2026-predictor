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

CST = timezone(timedelta(hours=8))

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


def analyze_match(match, elo_data, form_data, fetch_odds=False):
    """Full analysis for a single match."""
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

    # Win/draw/loss
    w1, d, w2 = calc_match_probabilities(elo1, elo2)

    # Form momentum adjustment
    fs1 = form_score(form1_str)
    fs2 = form_score(form2_str)
    adj = (fs1 - fs2) * 0.012
    w1 = max(0.02, min(0.98, w1 + adj))
    w2 = max(0.02, min(0.98, w2 - adj))
    d = 1.0 - w1 - w2

    # Handicap
    handicap_lines = [-0.5, -1.0, -1.5, -2.0]
    hcaps = []
    for hcap in handicap_lines:
        cov, nc, push = calc_handicap(elo1, elo2, hcap)
        hcaps.append({"line": hcap, "cover": cov, "not_cover": nc, "push": push})
    best_hcap = min(hcaps, key=lambda h: abs(h["cover"] - 0.5))

    # Scores
    scores = predict_scores(elo1, elo2)

    # Favorite
    if w1 > w2 + 0.05:
        favorite = team1
        fav_pct = w1
    elif w2 > w1 + 0.05:
        favorite = team2
        fav_pct = w2
    else:
        favorite = None

    # Live odds
    odds = None
    if fetch_odds:
        odds = fetch_live_odds(team1, team2, elo1, elo2)

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
        "form1": form1_str, "form2": form2_str,
        "form1_score": fs1, "form2_score": fs2,
        "favorite": favorite,
        "fav_pct": fav_pct if favorite else 0,
        "odds": odds,
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
                f"欧盘: 胜{eu.get('home','-')} / 平{eu.get('draw','-')} / 负{eu.get('away','-')}",
                f"状态: {c1} {fs1}({fd1})",
                f"      {c2} {fs2}({fd2})",
                f"ELO:  {c1} {r['elo1']} vs {c2} {r['elo2']} (差{r['elo1']-r['elo2']:+d})",
            ]),
            ("[ PREDICT 预测 ]", [
                f"{c1}胜 {w1:.0f}%   平 {d:.0f}%   {c2}胜 {w2:.0f}%",
                _hcap_text(c1, bh),
                _score_text(r["scores"]),
            ]),
        ]

        # Render
        label = f"{flag(t1)}{c1}vs{flag(t2)}{c2}"
        img_b64, img_md5 = _render_single_image(
            match_date_str, flag(t1), c1, flag(t2), c2,
            group, time_display, venue, rows_data, font_name
        )
        images.append((label, img_b64, img_md5))

    return images


def _hcap_text(c1, bh):
    hcap_line = f"{c1}{bh['line']:+.1f}"
    if bh["push"] > 0.01:
        return f"让球 {hcap_line}: 赢盘 {bh['cover']*100:.0f}% / 走水 {bh['push']*100:.0f}% / 输盘 {bh['not_cover']*100:.0f}%"
    return f"让球 {hcap_line}: 赢盘 {bh['cover']*100:.0f}% / 输盘 {bh['not_cover']*100:.0f}%"


def _score_text(scores):
    parts = [f"{g[0]}-{g[1]} ({g[2]*100:.1f}%)" for g in scores]
    return "比分: " + " / ".join(parts)


def _render_single_image(date_str, f1, c1, f2, c2, group, time_disp, venue, rows_data, font_name):
    """Render one match as a PNG image and return (base64, md5)."""
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.axis("off")
    fig.patch.set_facecolor("#1a1a2e")

    y = 1.0
    x_margin = 0.04

    # Title
    ax.text(x_margin, y, f"World Cup 世界杯预测 | {date_str}比赛日",
            fontsize=13, fontweight="bold", color="#e94560",
            fontfamily=font_name, transform=ax.transAxes, va="top")
    y -= 0.08

    # Match line
    ax.text(x_margin, y, f"{c1} vs {c2}",
            fontsize=12, fontweight="bold", color="#ffffff",
            fontfamily=font_name, transform=ax.transAxes, va="top")
    y -= 0.06
    ax.text(x_margin, y, f"{group} | {time_disp} | {venue}",
            fontsize=8, color="#aaaaaa",
            fontfamily=font_name, transform=ax.transAxes, va="top")
    y -= 0.10

    # Sections
    for section_title, rows in rows_data:
        # Section header
        ax.text(x_margin, y, section_title,
                fontsize=10, fontweight="bold", color="#f0c040",
                fontfamily=font_name, transform=ax.transAxes, va="top")
        y -= 0.01
        # Separator line
        ax.plot([x_margin, 0.96], [y + 0.005, y + 0.005],
                color="#333366", linewidth=0.5, transform=ax.transAxes, clip_on=False)
        y -= 0.06

        for row in rows:
            ax.text(x_margin + 0.02, y, row,
                    fontsize=8.5, color="#d0d0e0",
                    fontfamily=font_name, transform=ax.transAxes, va="top")
            y -= 0.055
        y -= 0.02

    # Footer
    ax.text(x_margin, 0.02, "ELO + Poisson + 近期状态  |  ELO隐含赔率  |  仅供参考",
            fontsize=6.5, color="#666688", fontfamily=font_name,
            transform=ax.transAxes, va="bottom")

    # Save to bytes
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor="#1a1a2e", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    img_bytes = buf.getvalue()

    img_b64 = base64.b64encode(img_bytes).decode("ascii")
    img_md5 = hashlib.md5(img_bytes).hexdigest()
    return img_b64, img_md5


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


def send_wechat_images(images, match_date_str):
    """Push prediction images to WeChat Work bot, one per match."""
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

    # Footer
    footer = {
        "msgtype": "markdown",
        "markdown": {"content": "> ⚠ ELO + Poisson + 近期状态 | ELO隐含赔率 | 仅供参考"},
    }
    try:
        requests.post(WEBHOOK_URL, json=footer, timeout=15)
    except Exception:
        pass


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


# ---------- main ----------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="World Cup 2026 Prediction Engine")
    parser.add_argument("--date", help="Target date YYYY-MM-DD (default: tomorrow CST)")
    parser.add_argument("--dry-run", action="store_true", help="Print only, no WeChat push")
    parser.add_argument("--no-odds", action="store_true", help="Skip odds fetch (faster)")
    args = parser.parse_args()

    print("=" * 56)
    print("  ⚽ World Cup 2026 Prediction Engine")
    print("=" * 56)

    all_matches, elo_data, form_data = load_data()
    print(f"[数据] {len(all_matches)}场比赛, "
          f"{len(elo_data['teams'])}队ELO, "
          f"{len(form_data['forms'])}条状态")

    if ODDS_API_KEY:
        print(f"[赔率] 实时赔率已启用 (the-odds-api)")
    else:
        print(f"[赔率] 使用ELO隐含赔率 (设置ODDS_API_KEY启用实时)")

    # Determine target date
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        target_str = target_date.strftime("%m月%d日")
    else:
        now_cst = datetime.now(CST)
        target_date = (now_cst + timedelta(days=1)).date()
        target_str = target_date.strftime("%m月%d日")

    target_matches = [
        m for m in all_matches
        if m.get("date") == target_date.strftime("%Y-%m-%d")
    ]

    if not target_matches:
        print(f"\n[信息] {target_str} 无比赛安排，跳过")
        return

    print(f"\n[赛程] {target_str} 共 {len(target_matches)} 场比赛")

    fetch_odds = not args.no_odds
    results = []
    for m in target_matches:
        t1, t2 = m["team1"], m["team2"]
        c1, c2 = cn(t1), cn(t2)
        bj_time, nd = to_beijing_time(m.get("time", ""))
        print(f"\n[分析] {c1} vs {c2} ({bj_time} 北京)")
        result = analyze_match(m, elo_data, form_data, fetch_odds=fetch_odds)
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

    send_wechat_images(images, target_str)
    print("✅ 推送完成")


if __name__ == "__main__":
    main()
