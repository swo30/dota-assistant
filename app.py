"""
Dota 2 Draft Assistant - Self Hosted
v2.0 - Now with Team Synergy, Draft Presets, Manual Mode, and Docker support
"""
import json
import os
import time
from typing import Dict, List, Optional, Set
from functools import lru_cache

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Dota 2 Draft Assistant v2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ──
OPENDOTA_BASE = "https://api.opendota.com/api"
CACHE_DIR = os.environ.get("CACHE_DIR", "./cache")
DATA_DIR = os.environ.get("DATA_DIR", "./data")
PLAYER_CACHE_DIR = os.path.join(CACHE_DIR, "players")
HERO_CACHE_FILE = os.path.join(CACHE_DIR, "heroes.json")
HERO_STATS_CACHE = os.path.join(CACHE_DIR, "hero_stats.json")
MATCHUP_CACHE_DIR = os.path.join(CACHE_DIR, "matchups")
SYNERGY_CACHE_FILE = os.path.join(CACHE_DIR, "synergy_dynamic.json")
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL", 3600 * 6))  # 6 hours default

# ── Minimum Games Filter ──
# Heroes with fewer than this many games won't be suggested for that player.
# Set to 0 to disable. Only applies to personal stats mode.
MIN_GAMES_THRESHOLD: int = 20

os.makedirs(PLAYER_CACHE_DIR, exist_ok=True)
os.makedirs(MATCHUP_CACHE_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# ── Load Static Data ──
SYNERGIES: Dict[str, float] = {}
PRESETS: Dict[str, dict] = {}

def load_static_data():
    global SYNERGIES, PRESETS
    try:
        with open(os.path.join(DATA_DIR, "synergies.json"), "r") as f:
            raw = json.load(f)
            SYNERGIES = raw.get("pairs", {})
    except Exception as e:
        print(f"Warning: Could not load synergies.json: {e}")
        SYNERGIES = {}

    try:
        with open(os.path.join(DATA_DIR, "presets.json"), "r") as f:
            PRESETS = json.load(f)
    except Exception as e:
        print(f"Warning: Could not load presets.json: {e}")
        PRESETS = {}

load_static_data()

# ── Load Default Team ──
DEFAULT_TEAM: List[dict] = []

def load_default_team():
    global DEFAULT_TEAM
    try:
        with open(os.path.join(DATA_DIR, "default_team.json"), "r") as f:
            raw = json.load(f)
            DEFAULT_TEAM = raw.get("players", [])
    except Exception as e:
        print(f"Warning: Could not load default_team.json: {e}")
        DEFAULT_TEAM = []

load_default_team()


# ── Position → Role Mapping ──
POSITION_ROLES = {
    1: ["Carry"],
    2: ["Carry", "Nuker", "Escape"],
    3: ["Initiator", "Durable", "Disabler", "Carry"],
    4: ["Support", "Disabler", "Initiator", "Escape"],
    5: ["Support", "Disabler", "Nuker"],
}

# ── Models ──
class PlayerSetup(BaseModel):
    account_id: Optional[int] = None  # None = manual / rando mode
    position: int  # 1-5
    nickname: str = ""

class EnemyPick(BaseModel):
    hero_id: int
    position: Optional[int] = None

class DraftState(BaseModel):
    team: List[PlayerSetup]
    enemy_picks: List[EnemyPick] = []
    bans: List[int] = []
    already_picked: List[int] = []
    preset_key: Optional[str] = None  # e.g. "team_global"
    use_manual: bool = False  # If True, ignore account_ids and use presets/manual
    position_overrides: Optional[Dict[str, int]] = None  # nickname -> position override for this draft

class HeroRecommendation(BaseModel):
    hero_id: int
    hero_name: str
    score: float
    player_comfort: float
    position_fit: float
    counter_score: float
    synergy_score: float
    meta_score: float
    preset_bonus: float
    games_played: int
    win_rate: float
    reason: str
    synergies_with: List[str]  # Names of team heroes this synergizes with
    best_for_player: str = ""  # Which player this is best for


# ── OpenDota Client ──
class OpenDotaClient:
    @staticmethod
    def _get(url: str, cache_file: Optional[str] = None, ttl: int = CACHE_TTL_SECONDS):
        if cache_file and os.path.exists(cache_file):
            age = time.time() - os.path.getmtime(cache_file)
            if age < ttl:
                with open(cache_file, 'r') as f:
                    return json.load(f)

        resp = requests.get(url, timeout=45)
        resp.raise_for_status()
        data = resp.json()

        if cache_file:
            os.makedirs(os.path.dirname(cache_file) or ".", exist_ok=True)
            with open(cache_file, 'w') as f:
                json.dump(data, f)
        return data

    @classmethod
    @lru_cache(maxsize=1)
    def get_heroes(cls) -> List[dict]:
        return cls._get(f"{OPENDOTA_BASE}/heroes", HERO_CACHE_FILE, ttl=86400*7)  # 7 days

    @classmethod
    def get_hero_stats(cls) -> List[dict]:
        return cls._get(f"{OPENDOTA_BASE}/heroStats", HERO_STATS_CACHE)

    @classmethod
    def get_player_heroes(cls, account_id: int) -> List[dict]:
        cache = os.path.join(PLAYER_CACHE_DIR, f"{account_id}_heroes.json")
        return cls._get(f"{OPENDOTA_BASE}/players/{account_id}/heroes", cache)

    @classmethod
    def get_player_counts(cls, account_id: int) -> dict:
        cache = os.path.join(PLAYER_CACHE_DIR, f"{account_id}_counts.json")
        return cls._get(f"{OPENDOTA_BASE}/players/{account_id}/counts", cache)

    @classmethod
    def get_hero_matchups(cls, hero_id: int) -> List[dict]:
        cache = os.path.join(MATCHUP_CACHE_DIR, f"{hero_id}.json")
        return cls._get(f"{OPENDOTA_BASE}/heroes/{hero_id}/matchups", cache)

    @classmethod
    def get_explorer(cls, sql: str) -> dict:
        """Query OpenDota SQL explorer. Use sparingly — it's heavy."""
        resp = requests.get(f"{OPENDOTA_BASE}/explorer", params={"sql": sql}, timeout=60)
        resp.raise_for_status()
        return resp.json()


# ── Data Helpers ──
def get_hero_by_id(hero_id: int) -> Optional[dict]:
    for h in OpenDotaClient.get_heroes():
        if h["id"] == hero_id:
            return h
    return None

def get_hero_name(hero_id: int) -> str:
    h = get_hero_by_id(hero_id)
    return h["localized_name"] if h else f"Hero-{hero_id}"

def get_meta_win_rate(hero_id: int) -> float:
    for h in OpenDotaClient.get_hero_stats():
        if h["id"] == hero_id:
            picks = h.get("pro_pick", 0) or h.get("public_pick", 0) or 1
            wins = h.get("pro_win", 0) or h.get("public_win", 0) or 0
            return wins / picks if picks > 0 else 0.5
    return 0.5

def get_counter_scores(enemy_hero_ids: List[int]) -> Dict[int, float]:
    """
    Returns {hero_id: counter_score} where counter_score is 0-1.
    Higher = better against the enemy team.
    """
    scores: Dict[int, List[float]] = {}
    for enemy_id in enemy_hero_ids:
        try:
            matchups = OpenDotaClient.get_hero_matchups(enemy_id)
            for m in matchups:
                target_id = m["hero_id"]
                games = m.get("games_played", 0)
                wins = m.get("wins", 0)
                if games < 10:
                    continue
                enemy_wr = wins / games
                counter_value = 1.0 - enemy_wr  # If enemy loses to this hero, it's a good counter
                if target_id not in scores:
                    scores[target_id] = []
                scores[target_id].append(counter_value)
        except Exception as e:
            print(f"Failed to fetch matchups for enemy {enemy_id}: {e}")
            continue

    # Average and normalize
    result = {}
    for hid, vals in scores.items():
        avg = sum(vals) / len(vals)
        result[hid] = min(max(avg, 0.3), 0.8) / 0.8  # Normalize 0.3-0.8 range to 0-1
    return result


def get_synergy_score(hero_id: int, team_hero_ids: List[int]) -> tuple[float, List[str]]:
    """
    Returns (synergy_score, list_of_synergy_partner_names).
    Checks static synergy database.
    """
    if not SYNERGIES:
        return 0.5, []

    scores = []
    partners = []
    for teammate_id in team_hero_ids:
        if teammate_id == hero_id:
            continue
        key1 = f"{hero_id}_{teammate_id}"
        key2 = f"{teammate_id}_{hero_id}"
        val = SYNERGIES.get(key1) or SYNERGIES.get(key2)
        if val:
            scores.append(val)
            partners.append(get_hero_name(teammate_id))

    if not scores:
        return 0.5, []

    avg = sum(scores) / len(scores)
    return avg, partners


def get_preset_bonus(hero_id: int, position: int, preset_key: Optional[str]) -> float:
    """Returns 0.0-0.3 bonus if hero is in the preset's preferred list for this position."""
    if not preset_key or preset_key not in PRESETS:
        return 0.0

    preset = PRESETS[preset_key]
    pos_str = str(position)
    preferred = preset.get("preferred_heroes", {}).get(pos_str, [])

    if hero_id in preferred:
        try:
            rank = preferred.index(hero_id)
            return max(0.3 - (rank * 0.05), 0.05)
        except ValueError:
            return 0.0
    return 0.0


# ── Draft Engine ──
class DraftEngine:
    def __init__(self):
        self.heroes = {h["id"]: h for h in OpenDotaClient.get_heroes()}
        self.hero_stats = {h["id"]: h for h in OpenDotaClient.get_hero_stats()}

    def score_hero_for_player(
        self,
        hero_id: int,
        player: PlayerSetup,
        player_hero_data: dict,
        enemy_picks: List[EnemyPick],
        team_picks: List[int],
        bans: List[int],
        preset_key: Optional[str],
        is_manual: bool,
    ) -> Optional[HeroRecommendation]:

        if hero_id in bans or hero_id in team_picks:
            return None

        hero = self.heroes.get(hero_id)
        if not hero:
            return None

        enemy_ids = [e.hero_id for e in enemy_picks]

        # ── Player Comfort ──
        games = player_hero_data.get("games", 0)
        wins = player_hero_data.get("win", 0)

        if is_manual or games == 0:
            wr = get_meta_win_rate(hero_id)
            comfort = wr * 0.8
            games = 0
        else:
            if MIN_GAMES_THRESHOLD > 0 and games > 0 and games < MIN_GAMES_THRESHOLD:
                wr = wins / games
                penalty = games / MIN_GAMES_THRESHOLD
                game_factor = min(max(0.3, (games ** 0.4) / 3), 2.0)
                comfort = wr * game_factor * penalty
            else:
                wr = wins / games if games > 0 else 0.45
                game_factor = min(max(0.3, (games ** 0.4) / 3), 2.0)
                comfort = wr * game_factor

        # ── Position Fit ──
        hero_roles = set(hero.get("roles", []))
        expected_roles = set(POSITION_ROLES.get(player.position, []))
        overlap = len(hero_roles & expected_roles)
        if overlap > 0:
            pos_fit = 0.7 + (0.1 * overlap)
        else:
            pos_fit = 0.3

        # ── Counter Score ──
        counter_scores = get_counter_scores(enemy_ids)
        counter = counter_scores.get(hero_id, 0.5)

        # ── Synergy Score ──
        synergy, synergy_partners = get_synergy_score(hero_id, team_picks)

        # ── Meta Strength ──
        meta = get_meta_win_rate(hero_id)

        # ── Preset Bonus ──
        preset_bonus = get_preset_bonus(hero_id, player.position, preset_key)

        # ── Final Score ──
        if is_manual:
            score = (
                comfort * 0.25 +
                pos_fit * 0.25 +
                counter * 0.20 +
                synergy * 0.15 +
                meta * 0.10 +
                preset_bonus * 1.0
            )
        else:
            score = (
                comfort * 0.35 +
                pos_fit * 0.20 +
                counter * 0.20 +
                synergy * 0.10 +
                meta * 0.10 +
                preset_bonus * 1.0
            )

        reasons = []
        if not is_manual and games > 20 and wr > 0.55:
            reasons.append(f"Comfort pick ({wr:.0%} WR, {games} games)")
        elif not is_manual and games > 5:
            reasons.append(f"Played {games} games")
        if counter > 0.6:
            reasons.append("Counters enemy")
        if synergy > 0.6:
            reasons.append(f"Synergy with {', '.join(synergy_partners[:2])}")
        if pos_fit > 0.8:
            reasons.append("Fits position")
        if meta > 0.52:
            reasons.append("Strong in meta")
        if preset_bonus > 0.15:
            reasons.append("Fits team strategy")

        return HeroRecommendation(
            hero_id=hero_id,
            hero_name=hero["localized_name"],
            score=round(score, 3),
            player_comfort=round(comfort, 3),
            position_fit=round(pos_fit, 3),
            counter_score=round(counter, 3),
            synergy_score=round(synergy, 3),
            meta_score=round(meta, 3),
            preset_bonus=round(preset_bonus, 3),
            games_played=games,
            win_rate=round(wr, 3),
            reason="; ".join(reasons) if reasons else "Solid all-rounder",
            synergies_with=synergy_partners,
        )

    def recommend(
        self,
        state: DraftState,
        top_n: int = 8,
    ) -> Dict[str, dict]:
        """
        Returns recommendations grouped by player nickname based strictly on their current dropdown position.
        """
        team_picks = list(state.already_picked)
        is_manual_mode = state.use_manual

        results = {}
        used_heroes = set(team_picks)

        for player in state.team:
            nickname = player.nickname.strip() or f"Player {player.position}"
            is_player_rando = nickname.lower() in ["rando", "random"]
            is_player_manual = is_manual_mode or is_player_rando or not player.account_id

            hero_stats_map = {}
            if not is_player_manual and player.account_id:
                try:
                    player_heroes = OpenDotaClient.get_player_heroes(int(player.account_id))
                    hero_stats_map = {ph["hero_id"]: ph for ph in player_heroes}
                except Exception as e:
                    print(f"Failed to fetch player {player.account_id}: {e}")

            player_scored = []
            for hero_id in self.heroes:
                rec = self.score_hero_for_player(
                    hero_id=hero_id,
                    player=player,
                    player_hero_data=hero_stats_map.get(hero_id, {}),
                    enemy_picks=state.enemy_picks,
                    team_picks=team_picks,
                    bans=state.bans,
                    preset_key=state.preset_key,
                    is_manual=is_player_manual,
                )
                if rec:
                    rec.reason = f"Pos {player.position} fit; {rec.reason}"
                    player_scored.append((rec.score, rec))

            player_scored.sort(key=lambda x: x[0], reverse=True)

            final_player_recs = []
            seen_heroes = set(used_heroes)

            for score, rec in player_scored:
                if rec.hero_id in seen_heroes or rec.hero_id in state.bans:
                    continue
                final_player_recs.append(rec)
                seen_heroes.add(rec.hero_id)
                if len(final_player_recs) >= top_n:
                    break

            results[nickname] = {
                "position": player.position,
                "recommendations": final_player_recs
            }

        return results


# ── API Routes ──
@app.get("/api/heroes")
def list_heroes():
    return OpenDotaClient.get_heroes()

@app.get("/api/presets")
def list_presets():
    return {
        key: {
            "name": val["name"],
            "description": val["description"],
        }
        for key, val in PRESETS.items()
    }

@app.get("/api/presets/{preset_key}")
def get_preset(preset_key: str):
    if preset_key not in PRESETS:
        raise HTTPException(404, "Preset not found")
    return PRESETS[preset_key]

@app.post("/api/players/{account_id}/refresh")
def refresh_player(account_id: int):
    for suffix in ["_heroes.json", "_counts.json"]:
        path = os.path.join(PLAYER_CACHE_DIR, f"{account_id}{suffix}")
        if os.path.exists(path):
            os.remove(path)
    try:
        heroes = OpenDotaClient.get_player_heroes(account_id)
        counts = OpenDotaClient.get_player_counts(account_id)
        return {"status": "ok", "heroes_cached": len(heroes), "counts_cached": bool(counts)}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/players/{account_id}/heroes")
def get_player_heroes(account_id: int):
    return OpenDotaClient.get_player_heroes(account_id)

@app.post("/api/draft/recommend")
def recommend_draft(state: DraftState):
    engine = DraftEngine()
    return engine.recommend(state, top_n=8)

@app.post("/api/synergy/fetch")
def fetch_synergy(hero_a: int = Query(...), hero_b: int = Query(...)):
    sql = f"""
    SELECT
        COUNT(*) as games,
        SUM(CASE WHEN (radiant_win = TRUE AND pm1.player_slot < 128) OR (radiant_win = FALSE AND pm1.player_slot >= 128) THEN 1 ELSE 0 END) as wins
    FROM player_matches pm1
    JOIN player_matches pm2 ON pm1.match_id = pm2.match_id
    JOIN matches ON pm1.match_id = matches.match_id
    WHERE pm1.hero_id = {hero_a}
      AND pm2.hero_id = {hero_b}
      AND pm1.player_slot < 128
      AND pm2.player_slot < 128
      AND pm1.account_id != pm2.account_id
      AND matches.start_time > extract(epoch from now() - interval '6 month')::int
    """
    try:
        result = OpenDotaClient.get_explorer(sql)
        rows = result.get("rows", [{}])[0]
        games = rows.get("games", 0)
        wins = rows.get("wins", 0)
        wr = wins / games if games > 0 else 0.5
        return {
            "hero_a": get_hero_name(hero_a),
            "hero_b": get_hero_name(hero_b),
            "games": games,
            "wins": wins,
            "win_rate": round(wr, 3),
            "synergy_score": round((wr - 0.5) * 2 + 0.5, 3),
        }
    except Exception as e:
        raise HTTPException(500, f"Explorer query failed: {e}")

@app.get("/api/team/default")
def get_default_team():
    return {
        "players": [
            {
                "account_id": p.get("account_id"),
                "nickname": p.get("nickname", "Player"),
                "default_position": p.get("default_position", 1),
                "notes": p.get("notes", "")
            }
            for p in DEFAULT_TEAM
        ]
    }

@app.get("/api/health")
def health():
    return {"status": "ok", "heroes_loaded": len(OpenDotaClient.get_heroes()), "presets_loaded": len(PRESETS)}

app.mount("/", StaticFiles(directory="static", html=True), name="static")