"""Theme catalog + on-topic checks for Decoy rounds.

Source of truth for:
  • lobby theme chips (group id → member topic slugs)
  • whether a post body actually fits its tagged topic

Server round pick and refill tooling import this so the picker and the
builder stay aligned.
"""

from __future__ import annotations

import re
from typing import Any

# Broad lobby chips. Keep labels short; member topics are the real filter.
TOPIC_CATALOG: list[dict[str, Any]] = [
    {"id": "random", "label": "RANDOM", "blurb": "any topic", "topics": []},
    {
        "id": "technology",
        "label": "TECHNOLOGY",
        "blurb": "AI · tech · startups · crypto",
        "topics": ["ai", "tech", "startups", "crypto"],
    },
    {
        "id": "movies_tv",
        "label": "MOVIES & TV",
        "blurb": "films · series · streaming",
        "topics": ["movies", "tv"],
    },
    {
        "id": "music",
        "label": "MUSIC",
        "blurb": "artists · albums · concerts",
        "topics": ["music"],
    },
    {
        "id": "gaming",
        "label": "GAMING",
        "blurb": "games · studios · esports",
        "topics": ["gaming"],
    },
    {
        "id": "sports",
        "label": "SPORTS",
        "blurb": "NBA · soccer · baseball",
        "topics": ["sports", "nba", "baseball", "soccer"],
    },
    {
        "id": "science",
        "label": "SCIENCE & SPACE",
        "blurb": "research · NASA · cosmos",
        "topics": ["science", "space"],
    },
    {
        "id": "lifestyle",
        "label": "LIFESTYLE",
        "blurb": "food · travel · fitness · cars",
        "topics": ["food", "travel", "fitness", "cars", "books", "photography"],
    },
]

# Legacy group ids from older clients → current catalog ids.
LEGACY_GROUP_IDS: dict[str, str] = {
    "entertainment": "movies_tv",  # old mega-bucket; map to movies/tv core
}

# Positive signals: post should hit at least one for its tagged topic.
# Keep patterns inclusive — names/teams count as much as the sport word.
_TOPIC_POSITIVE: dict[str, re.Pattern[str]] = {
    "ai": re.compile(
        r"\b(ai|a\.i\.|gpt|llm|openai|anthropic|claude|gemini|grok|chatgpt|"
        r"machine learning|neural|chatbot|language model|diffusion|"
        r"agentic|copilot|midjourney|stable diffusion|coding agent|"
        r"muse spark|model(?:s)?\b|llm)\b",
        re.I,
    ),
    "tech": re.compile(
        r"\b(iphone|android|apple|samsung|gadget|software|app\b|phone|"
        r"hardware|tech|chip|gpu|laptop|pixel|magsafe|one ui|ios|macos|"
        r"windows|chrome|browser|startup|saas|api|device)\b",
        re.I,
    ),
    "startups": re.compile(
        r"\b(startup|founder|seed round|series [a-c]|saas|launch|"
        r"paying customers|mrr|arr|yc\b|product hunt|venture|raised)\b",
        re.I,
    ),
    "crypto": re.compile(
        r"\b(crypto|bitcoin|btc|ethereum|eth\b|solana|sol\b|blockchain|"
        r"on-?chain|defi|nft|token|binance|coinbase|market cap)\b",
        re.I,
    ),
    "movies": re.compile(
        r"\b(movies?|films?|cinema|box office|trailer|director|actors?|"
        r"actress(?:es)?|sequels?|cinematograph\w*|oscars?|blockbusters?|"
        r"screenplay|spider-?man|star wars|odyssey|kong)\b",
        re.I,
    ),
    "tv": re.compile(
        r"\b(tv|series|season|episode|finale|streaming|netflix|hulu|"
        r"disney\+|apple tv|hbo|max\b|showrunner|sitcom|x-?men|"
        r"hotd|house of the dragon|digital circus|cape fear)\b",
        re.I,
    ),
    "music": re.compile(
        r"\b(music|album|song|rap|rapper|concert|tour|spotify|apple music|"
        r"billboard|grunge|drake|kendrick|artist|single|vinyl|dj\b|"
        r"morrissey|massive attack)\b",
        re.I,
    ),
    "gaming": re.compile(
        r"\b(game|gaming|gamer|steam|xbox|playstation|nintendo|esports|"
        r"gta|gears of war|goty|studio|trailer|early access|console|"
        r"pc game|multiplayer|fps|rpg)\b",
        re.I,
    ),
    "memes": re.compile(
        r"\b(meme|memes|shitpost|copypasta|viral|format|drake format)\b",
        re.I,
    ),
    "sports": re.compile(
        r"\b(sports?|transfer|match|fixture|league|cup|coach|striker|"
        r"midfield|goalkeeper|premier league|serie a|la liga|champions|"
        r"arsenal|liverpool|chelsea|tottenham|barcelona|madrid|united|"
        r"manchester|bayern|salah|trabzonspor|nba|mlb|nfl|soccer|"
        r"football|basketball|baseball)\b",
        re.I,
    ),
    "nba": re.compile(
        r"\b(nba|basketball|lakers|celtics|nuggets|bucks|pacers|warriors|"
        r"lebron|jokic|wemby|reaves|kuminga|playoff|finals|trade|"
        r"all-?star|three-?point|dunk)\b",
        re.I,
    ),
    "baseball": re.compile(
        r"\b(mlb|baseball|yankees|dodgers|twins|phillies|orioles|o'?s\b|"
        r"pitcher|batter|home run|inning|world series|strikeout|k['’]?s|"
        r"bohm|walter johnson|neto|trade deadline|walker jenkins)\b",
        re.I,
    ),
    "soccer": re.compile(
        r"\b(soccer|football|premier league|la liga|serie a|bundesliga|"
        r"champions league|fifa|transfer|striker|midfielder|goalkeeper|"
        r"arsenal|liverpool|chelsea|tottenham|barcelona|madrid|united|"
        r"manchester|bayern|mourinho|arteta|salah|mac allister)\b",
        re.I,
    ),
    "science": re.compile(
        r"\b(science|research|physics|biology|quantum|particle|species|"
        r"evolution|larva|fossil|climate science|study|scientists?|"
        r"experiment|genome|chemistry|impact|tonnes of rock|underwater|"
        r"magnetic monopole|ballistic)\b",
        re.I,
    ),
    "space": re.compile(
        r"\b(space|nasa|mars|moon|rocket|orbit|galaxy|telescope|astronaut|"
        r"eclipse|solar|planet|spacecraft|light-?years?|astronomy|"
        r"satellite|spacex|starship)\b",
        re.I,
    ),
    "food": re.compile(
        r"\b(food|restaurant|recipe|cook|cooking|chef|meal|olive oil|"
        r"cuisine|dinner|lunch|kitchen|menu|eat|dining)\b",
        re.I,
    ),
    "travel": re.compile(
        r"\b(travel|trip|flight|airline|hotel|beach|vacation|tourist|"
        r"destination|passport|luggage|airport|resort|backpack)\b",
        re.I,
    ),
    "fitness": re.compile(
        r"\b(fitness|gym|lift|lifting|workout|training|run|running|"
        r"pr\b|deadlift|squat|cardio|athlete|home gym)\b",
        re.I,
    ),
    "cars": re.compile(
        r"\b(car|cars|auto|ev\b|tesla|nissan|datsun|toyota|bmw|ford|"
        r"vehicle|engine|racing|motorsport|hybrid|dealer)\b",
        re.I,
    ),
    "books": re.compile(
        r"\b(book|books|author|novel|reader|reading|publishing|genre|"
        r"writing community|bookstore|paperback|hardcover)\b",
        re.I,
    ),
    "photography": re.compile(
        r"\b(photo|photography|photographer|camera|lens|sony|canon|nikon|"
        r"aperture|iso|shutter|portrait|shoot)\b",
        re.I,
    ),
}

# Hard cross-theme pollution: if these fire and positives don't, reject.
_TOPIC_NEGATIVE: dict[str, re.Pattern[str]] = {
    "ai": re.compile(
        r"\b(nba finals?|premier league|box office|album drop|gta\s*6)\b", re.I
    ),
    "tech": re.compile(
        r"\b(nba finals?|premier league|transfer window|box office)\b", re.I
    ),
    "movies": re.compile(
        r"\b(nba|premier league|bitcoin|iphone\s*1[78]|steam deck)\b", re.I
    ),
    "tv": re.compile(
        r"\b(nba trade|premier league|bitcoin price|iphone\s*1[78])\b", re.I
    ),
    "music": re.compile(
        r"\b(nba finals?|premier league|iphone\s*1[78]|box office)\b", re.I
    ),
    "gaming": re.compile(
        r"\b(nba finals?|premier league|bitcoin|olive oil)\b", re.I
    ),
    "nba": re.compile(
        r"\b(premier league|arsenal|iphone|openai|gta\s*6|album)\b", re.I
    ),
    "soccer": re.compile(
        r"\b(nba|lakers|iphone|openai|gta\s*6|box office)\b", re.I
    ),
    "baseball": re.compile(
        r"\b(nba finals?|premier league|iphone|openai|gta\s*6)\b", re.I
    ),
    "sports": re.compile(
        r"\b(iphone\s*1[78]|openai|gta\s*6|box office|bitcoin)\b", re.I
    ),
    "science": re.compile(
        r"\b(nba|premier league|gta\s*6|iphone|album chart)\b", re.I
    ),
    "space": re.compile(
        r"\b(nba|premier league|gta\s*6|album chart)\b", re.I
    ),
    "food": re.compile(
        r"\b(nba|premier league|gta\s*6|bitcoin)\b", re.I
    ),
    "travel": re.compile(
        r"\b(nba finals?|premier league|gta\s*6|bitcoin)\b", re.I
    ),
}

# Round ids that are tagged wrong or too weak to represent their theme.
QUARANTINE_ROUND_IDS: frozenset[str] = frozenset(
    {
        # Anime character dump filed under "memes" — not a general meme post.
        "decoy-b56b4cea61ba",
    }
)


def catalog_groups() -> list[dict[str, Any]]:
    return [dict(g) for g in TOPIC_CATALOG]


def all_member_topics() -> set[str]:
    out: set[str] = set()
    for g in TOPIC_CATALOG:
        for t in g.get("topics") or []:
            out.add(str(t).lower())
    return out


def expand_group_id(group_id: str) -> list[str]:
    """Map a chip id (or legacy id) to member topic slugs."""
    key = str(group_id or "").strip().lower()
    if not key or key in ("random", "all", "*", "any"):
        return []
    key = LEGACY_GROUP_IDS.get(key, key)
    for g in TOPIC_CATALOG:
        if str(g["id"]).lower() == key:
            return [str(t).lower() for t in (g.get("topics") or []) if t]
    return []


def group_label_for_topics(topics: list[str] | set[str]) -> str:
    """Human label for a room's topic_filter (prefer group names over slugs)."""
    want = {str(t).lower() for t in topics if t}
    if not want:
        return "RANDOM MIX"
    labels: list[str] = []
    covered: set[str] = set()
    for g in TOPIC_CATALOG:
        members = {str(t).lower() for t in (g.get("topics") or []) if t}
        if not members:
            continue
        if members <= want:
            labels.append(str(g.get("label") or g["id"]).upper())
            covered |= members
    leftover = sorted(want - covered)
    for t in leftover:
        labels.append(t.upper())
    return " · ".join(labels) if labels else "RANDOM MIX"


def post_text_of(rnd: dict[str, Any]) -> str:
    src = rnd.get("source") if isinstance(rnd, dict) else None
    if isinstance(src, dict):
        return str(src.get("post_text") or "")
    return ""


def round_topic_of(rnd: dict[str, Any]) -> str:
    src = rnd.get("source") if isinstance(rnd, dict) else None
    if isinstance(src, dict):
        return str(src.get("topic") or "").strip().lower()
    return ""


def round_id_of(rnd: dict[str, Any]) -> str:
    if not isinstance(rnd, dict):
        return ""
    return str(rnd.get("round_id") or "").strip()


def is_quarantined(rnd: dict[str, Any]) -> bool:
    rid = round_id_of(rnd)
    if rid in QUARANTINE_ROUND_IDS:
        return True
    # Also match short suffix form decoy_topic_hex without prefix variants
    short = rid.replace("decoy-", "").replace("decoy_", "")
    for q in QUARANTINE_ROUND_IDS:
        if short and short in q.replace("decoy-", ""):
            return True
    return False


def theme_fit_score(topic: str, text: str) -> int:
    """Score how well post text fits a topic tag.

    2 = positive keyword hit
    1 = neutral (no signal — allowed when pool is thin)
    0 = negative cross-theme hit without positives (reject when alternatives exist)
    """
    topic = str(topic or "").strip().lower()
    body = str(text or "")
    if not topic or not body.strip():
        return 0
    pos = _TOPIC_POSITIVE.get(topic)
    neg = _TOPIC_NEGATIVE.get(topic)
    hit_pos = bool(pos.search(body)) if pos is not None else True
    hit_neg = bool(neg.search(body)) if neg is not None else False
    if hit_pos:
        return 2
    if hit_neg:
        return 0
    # Unknown topic slug or no lexicon — neutral, don't block.
    if pos is None:
        return 1
    return 1


def round_theme_score(rnd: dict[str, Any]) -> int:
    if is_quarantined(rnd):
        return 0
    return theme_fit_score(round_topic_of(rnd), post_text_of(rnd))


def round_fits_theme(rnd: dict[str, Any], *, min_score: int = 1) -> bool:
    """True when the round may be served under its own topic tag."""
    if is_quarantined(rnd):
        return False
    return round_theme_score(rnd) >= min_score
