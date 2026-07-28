"""Closed membership lists for the leagues with a fixed set of clubs.

Each entry is ``(abbr, name, city, nickname)``.  ``abbr`` is the stable identity
that goes into ``event_key``; changing one changes cross-source joining, so they
are chosen to match the abbreviations the books themselves use where those are
visible (Kambi sends ``"JAX Jaguars"``, ``"VGS Golden Knights"``, ``"NY
Rangers"``).

These tables were written from **live payloads of the three books**, not from
memory: the roster probe in the session that added each league printed the union
of participant names across FanDuel, Pinnacle and Kambi, and the tables below
cover that union.  Clubs absent from a given day's slate (the Chicago Sky were
not playing when the WNBA table was written) are included so a name does not
fail to resolve the first time that club appears.

Rosters are deliberately **per league**, never merged into one index.  Merging
them would make "Rangers" mean both the Texas Rangers and the New York Rangers,
"Panthers" both Carolina and Florida, "Kings" both Los Angeles and Sacramento,
and "Jets" both New York and Winnipeg.  A cross-sport false match is the worst
kind, because the two events are unrelated and their prices are unconstrained —
which is precisely the shape of a phantom arbitrage.
"""
from __future__ import annotations

#: ``(abbr, name, city, nickname)``.  An empty city means the club's canonical
#: name has none (the Athletics, after leaving Oakland).
RosterRow = tuple[str, str, str, str]

MLB: tuple[RosterRow, ...] = (
    ("ARI", "Arizona Diamondbacks", "Arizona", "Diamondbacks"),
    ("ATH", "Athletics", "", "Athletics"),
    ("ATL", "Atlanta Braves", "Atlanta", "Braves"),
    ("BAL", "Baltimore Orioles", "Baltimore", "Orioles"),
    ("BOS", "Boston Red Sox", "Boston", "Red Sox"),
    ("CHC", "Chicago Cubs", "Chicago", "Cubs"),
    ("CIN", "Cincinnati Reds", "Cincinnati", "Reds"),
    ("CLE", "Cleveland Guardians", "Cleveland", "Guardians"),
    ("COL", "Colorado Rockies", "Colorado", "Rockies"),
    ("CWS", "Chicago White Sox", "Chicago", "White Sox"),
    ("DET", "Detroit Tigers", "Detroit", "Tigers"),
    ("HOU", "Houston Astros", "Houston", "Astros"),
    ("KC", "Kansas City Royals", "Kansas City", "Royals"),
    ("LAA", "Los Angeles Angels", "Los Angeles", "Angels"),
    ("LAD", "Los Angeles Dodgers", "Los Angeles", "Dodgers"),
    ("MIA", "Miami Marlins", "Miami", "Marlins"),
    ("MIL", "Milwaukee Brewers", "Milwaukee", "Brewers"),
    ("MIN", "Minnesota Twins", "Minnesota", "Twins"),
    ("NYM", "New York Mets", "New York", "Mets"),
    ("NYY", "New York Yankees", "New York", "Yankees"),
    ("PHI", "Philadelphia Phillies", "Philadelphia", "Phillies"),
    ("PIT", "Pittsburgh Pirates", "Pittsburgh", "Pirates"),
    ("SD", "San Diego Padres", "San Diego", "Padres"),
    ("SEA", "Seattle Mariners", "Seattle", "Mariners"),
    ("SF", "San Francisco Giants", "San Francisco", "Giants"),
    ("STL", "St. Louis Cardinals", "St. Louis", "Cardinals"),
    ("TB", "Tampa Bay Rays", "Tampa Bay", "Rays"),
    ("TEX", "Texas Rangers", "Texas", "Rangers"),
    ("TOR", "Toronto Blue Jays", "Toronto", "Blue Jays"),
    ("WSH", "Washington Nationals", "Washington", "Nationals"),
)

NFL: tuple[RosterRow, ...] = (
    ("ARI", "Arizona Cardinals", "Arizona", "Cardinals"),
    ("ATL", "Atlanta Falcons", "Atlanta", "Falcons"),
    ("BAL", "Baltimore Ravens", "Baltimore", "Ravens"),
    ("BUF", "Buffalo Bills", "Buffalo", "Bills"),
    ("CAR", "Carolina Panthers", "Carolina", "Panthers"),
    ("CHI", "Chicago Bears", "Chicago", "Bears"),
    ("CIN", "Cincinnati Bengals", "Cincinnati", "Bengals"),
    ("CLE", "Cleveland Browns", "Cleveland", "Browns"),
    ("DAL", "Dallas Cowboys", "Dallas", "Cowboys"),
    ("DEN", "Denver Broncos", "Denver", "Broncos"),
    ("DET", "Detroit Lions", "Detroit", "Lions"),
    ("GB", "Green Bay Packers", "Green Bay", "Packers"),
    ("HOU", "Houston Texans", "Houston", "Texans"),
    ("IND", "Indianapolis Colts", "Indianapolis", "Colts"),
    ("JAX", "Jacksonville Jaguars", "Jacksonville", "Jaguars"),
    ("KC", "Kansas City Chiefs", "Kansas City", "Chiefs"),
    ("LAC", "Los Angeles Chargers", "Los Angeles", "Chargers"),
    ("LAR", "Los Angeles Rams", "Los Angeles", "Rams"),
    ("LV", "Las Vegas Raiders", "Las Vegas", "Raiders"),
    ("MIA", "Miami Dolphins", "Miami", "Dolphins"),
    ("MIN", "Minnesota Vikings", "Minnesota", "Vikings"),
    ("NE", "New England Patriots", "New England", "Patriots"),
    ("NO", "New Orleans Saints", "New Orleans", "Saints"),
    ("NYG", "New York Giants", "New York", "Giants"),
    ("NYJ", "New York Jets", "New York", "Jets"),
    ("PHI", "Philadelphia Eagles", "Philadelphia", "Eagles"),
    ("PIT", "Pittsburgh Steelers", "Pittsburgh", "Steelers"),
    ("SEA", "Seattle Seahawks", "Seattle", "Seahawks"),
    ("SF", "San Francisco 49ers", "San Francisco", "49ers"),
    ("TB", "Tampa Bay Buccaneers", "Tampa Bay", "Buccaneers"),
    ("TEN", "Tennessee Titans", "Tennessee", "Titans"),
    ("WAS", "Washington Commanders", "Washington", "Commanders"),
)

NHL: tuple[RosterRow, ...] = (
    ("ANA", "Anaheim Ducks", "Anaheim", "Ducks"),
    ("BOS", "Boston Bruins", "Boston", "Bruins"),
    ("BUF", "Buffalo Sabres", "Buffalo", "Sabres"),
    ("CAR", "Carolina Hurricanes", "Carolina", "Hurricanes"),
    ("CBJ", "Columbus Blue Jackets", "Columbus", "Blue Jackets"),
    ("CGY", "Calgary Flames", "Calgary", "Flames"),
    ("CHI", "Chicago Blackhawks", "Chicago", "Blackhawks"),
    ("COL", "Colorado Avalanche", "Colorado", "Avalanche"),
    ("DAL", "Dallas Stars", "Dallas", "Stars"),
    ("DET", "Detroit Red Wings", "Detroit", "Red Wings"),
    ("EDM", "Edmonton Oilers", "Edmonton", "Oilers"),
    ("FLA", "Florida Panthers", "Florida", "Panthers"),
    ("LA", "Los Angeles Kings", "Los Angeles", "Kings"),
    ("MIN", "Minnesota Wild", "Minnesota", "Wild"),
    ("MTL", "Montreal Canadiens", "Montreal", "Canadiens"),
    ("NJ", "New Jersey Devils", "New Jersey", "Devils"),
    ("NSH", "Nashville Predators", "Nashville", "Predators"),
    ("NYI", "New York Islanders", "New York", "Islanders"),
    ("NYR", "New York Rangers", "New York", "Rangers"),
    ("OTT", "Ottawa Senators", "Ottawa", "Senators"),
    ("PHI", "Philadelphia Flyers", "Philadelphia", "Flyers"),
    ("PIT", "Pittsburgh Penguins", "Pittsburgh", "Penguins"),
    ("SEA", "Seattle Kraken", "Seattle", "Kraken"),
    ("SJ", "San Jose Sharks", "San Jose", "Sharks"),
    ("STL", "St. Louis Blues", "St. Louis", "Blues"),
    ("TB", "Tampa Bay Lightning", "Tampa Bay", "Lightning"),
    ("TOR", "Toronto Maple Leafs", "Toronto", "Maple Leafs"),
    ("UTA", "Utah Mammoth", "Utah", "Mammoth"),
    ("VAN", "Vancouver Canucks", "Vancouver", "Canucks"),
    ("VGK", "Vegas Golden Knights", "Vegas", "Golden Knights"),
    ("WPG", "Winnipeg Jets", "Winnipeg", "Jets"),
    ("WSH", "Washington Capitals", "Washington", "Capitals"),
)

NBA: tuple[RosterRow, ...] = (
    ("ATL", "Atlanta Hawks", "Atlanta", "Hawks"),
    ("BKN", "Brooklyn Nets", "Brooklyn", "Nets"),
    ("BOS", "Boston Celtics", "Boston", "Celtics"),
    ("CHA", "Charlotte Hornets", "Charlotte", "Hornets"),
    ("CHI", "Chicago Bulls", "Chicago", "Bulls"),
    ("CLE", "Cleveland Cavaliers", "Cleveland", "Cavaliers"),
    ("DAL", "Dallas Mavericks", "Dallas", "Mavericks"),
    ("DEN", "Denver Nuggets", "Denver", "Nuggets"),
    ("DET", "Detroit Pistons", "Detroit", "Pistons"),
    ("GSW", "Golden State Warriors", "Golden State", "Warriors"),
    ("HOU", "Houston Rockets", "Houston", "Rockets"),
    ("IND", "Indiana Pacers", "Indiana", "Pacers"),
    ("LAC", "Los Angeles Clippers", "Los Angeles", "Clippers"),
    ("LAL", "Los Angeles Lakers", "Los Angeles", "Lakers"),
    ("MEM", "Memphis Grizzlies", "Memphis", "Grizzlies"),
    ("MIA", "Miami Heat", "Miami", "Heat"),
    ("MIL", "Milwaukee Bucks", "Milwaukee", "Bucks"),
    ("MIN", "Minnesota Timberwolves", "Minnesota", "Timberwolves"),
    ("NOP", "New Orleans Pelicans", "New Orleans", "Pelicans"),
    ("NYK", "New York Knicks", "New York", "Knicks"),
    ("OKC", "Oklahoma City Thunder", "Oklahoma City", "Thunder"),
    ("ORL", "Orlando Magic", "Orlando", "Magic"),
    ("PHI", "Philadelphia 76ers", "Philadelphia", "76ers"),
    ("PHX", "Phoenix Suns", "Phoenix", "Suns"),
    ("POR", "Portland Trail Blazers", "Portland", "Trail Blazers"),
    ("SAC", "Sacramento Kings", "Sacramento", "Kings"),
    ("SAS", "San Antonio Spurs", "San Antonio", "Spurs"),
    ("TOR", "Toronto Raptors", "Toronto", "Raptors"),
    ("UTA", "Utah Jazz", "Utah", "Jazz"),
    ("WAS", "Washington Wizards", "Washington", "Wizards"),
)

#: The WNBA as constituted for the 2026 season, including the two clubs that
#: joined that year (Toronto Tempo, Portland Fire).
WNBA: tuple[RosterRow, ...] = (
    ("ATL", "Atlanta Dream", "Atlanta", "Dream"),
    ("CHI", "Chicago Sky", "Chicago", "Sky"),
    ("CON", "Connecticut Sun", "Connecticut", "Sun"),
    ("DAL", "Dallas Wings", "Dallas", "Wings"),
    ("GSV", "Golden State Valkyries", "Golden State", "Valkyries"),
    ("IND", "Indiana Fever", "Indiana", "Fever"),
    ("LAS", "Los Angeles Sparks", "Los Angeles", "Sparks"),
    ("LVA", "Las Vegas Aces", "Las Vegas", "Aces"),
    ("MIN", "Minnesota Lynx", "Minnesota", "Lynx"),
    ("NYL", "New York Liberty", "New York", "Liberty"),
    ("PHO", "Phoenix Mercury", "Phoenix", "Mercury"),
    ("POR", "Portland Fire", "Portland", "Fire"),
    ("SEA", "Seattle Storm", "Seattle", "Storm"),
    ("TOR", "Toronto Tempo", "Toronto", "Tempo"),
    ("WAS", "Washington Mystics", "Washington", "Mystics"),
)


#: Spellings the city-plus-nickname rule cannot derive: former names,
#: colloquialisms, and book-specific shorthands.  Keys are normalized
#: (lowercase, alphanumeric-only) and scoped to one roster.
EXTRA_ALIASES: dict[str, dict[str, str]] = {
    "mlb": {
        "oaklandathletics": "ATH",
        "oaklandas": "ATH",
        "sacramentoathletics": "ATH",
        "clevelandindians": "CLE",
        "stlouiscardinals": "STL",
        "saintlouiscardinals": "STL",
        "arizonadbacks": "ARI",
        "dbacks": "ARI",
        "washingtonnats": "WSH",
        "nats": "WSH",
        "anaheimangels": "LAA",
        "losangelesangelsofanaheim": "LAA",
        "tampabaydevilrays": "TB",
    },
    "nfl": {
        # Kambi abbreviations that are not the canonical ones.
        "wshcommanders": "WAS",
        "washingtonfootballteam": "WAS",
        "washingtonredskins": "WAS",
        "oaklandraiders": "LV",
        "sandiegochargers": "LAC",
        "stlouisrams": "LAR",
        "niners": "SF",
    },
    "nhl": {
        # Kambi sends "VGS Golden Knights"; the club's own abbreviation is VGK.
        "vgsgoldenknights": "VGK",
        "vgs": "VGK",
        "lasvegasgoldenknights": "VGK",
        "arizonacoyotes": "UTA",
        "utahhockeyclub": "UTA",
        "montrealcanadien": "MTL",
    },
    "nba": {
        "laclippers": "LAC",
        "philadelphia76ers": "PHI",
        "sixers": "PHI",
        "golenstatewarriors": "GSW",
    },
    "wnba": {
        "connecticutsuns": "CON",
        "lasvegasace": "LVA",
    },
}

#: Fragments that name more than one club in a roster, or that are too short to
#: be an identity, and must never resolve.  Collision detection in
#: :mod:`src.participants` already drops any form that maps to two clubs; these
#: are listed so the intent survives a future edit to the tables, and to cover
#: abbreviation-like fragments that are not derived from a city or nickname.
AMBIGUOUS: dict[str, frozenset[str]] = {
    "mlb": frozenset({"chicago", "new york", "ny", "los angeles", "la", "sox", "chi", "mlb"}),
    "nfl": frozenset({"new york", "ny", "los angeles", "la", "nfl"}),
    "nhl": frozenset({"new york", "ny", "nhl"}),
    "nba": frozenset({"los angeles", "la", "nba"}),
    "wnba": frozenset({"wnba"}),
}

#: Roster name -> membership table.
ROSTERS: dict[str, tuple[RosterRow, ...]] = {
    "mlb": MLB,
    "nfl": NFL,
    "nhl": NHL,
    "nba": NBA,
    "wnba": WNBA,
}
