"""What language a channel broadcasts in.

Plex's guide has no language field. Not on the list items, not on the full
metadata (checked on a live server). The only signal is the text itself: a
Spanish channel lists "Fútbol Americano NFL" and describes the game in Spanish.

So this counts common function words ("the", "el", "der") in the titles and
summaries a channel carries, for the six languages the setup offers. It is
pure Python with no dependency, and it answers None rather than guess: too
little text, or no clear winner, is "unknown". The person can always set the
language by hand, and that choice wins over this one.
"""
import re
import unicodedata

# Code, name in its own language (for the language picker), name in English
# (for "Detected: Spanish").
LANGUAGES = {
    "en": ("English", "English"),
    "es": ("Español", "Spanish"),
    "fr": ("Français", "French"),
    "pt": ("Português", "Portuguese"),
    "de": ("Deutsch", "German"),
    "it": ("Italiano", "Italian"),
}
DEFAULT = "en"

# Below these the text says too little to call. A channel with a week of
# guide has thousands of words, so these only matter for a thin guide.
MIN_WORDS = 40        # words seen in all
MIN_HITS = 10         # stopwords of the winning language
MIN_SHARE = 0.08      # of all words, so a name-heavy listing is not enough
MARGIN = 2.0          # the winner needs this many times the runner-up
DECISIVE = 4.0        # and this many to overturn a language already found


# Accents are folded away before counting, so "está" and "esta" are one word.
# Short ambiguous words ("a", "e", "o") are left out on purpose.
_STOP = {
    "en": "the of and to in is that it for on with as was at by this from or be are "
          "his her they their have has had but not which will been who were can its "
          "you one more after before into over against when while than them there what where".split(),
    "es": "un una unos unas que y en es por con para como pero "
          "mas muy su sus se lo al este esta estos estas son fue ser entre sobre "
          "tambien cuando donde hasta desde sin hay ya si les mi tu nos otro otra "
          "todo todos han era ha ante tras durante".split(),
    "fr": "les un une des que et en est dans pour par avec sur pas plus "
          "mais comme son sa ses au aux ce cette ces sont ont il elle nous vous ils "
          "elles qui ete etre tout tous fait aussi apres avant entre sans sous "
          "contre dont ou".split(),
    "pt": "os as um uma uns umas dos das em no na nos nas que com para por "
          "mas muito seu sua seus suas tambem quando onde sobre como nao sao foi ser "
          "esta entre ate desde sem ha ja ou ao aos mais pelo pela pelos pelas "
          "isso essa esse ela ele".split(),
    "de": "der die das den dem des ein eine einen einem einer und ist nicht mit von "
          "zu auf fur im sich auch als an aus bei nach uber wie oder wird sind war "
          "hat haben dass noch nur zum zur vom beim sie er es wir ihr".split(),
    "it": "gli un uno una della dei delle che in con per su "
          "sono non piu ma come anche nel nella nei nelle al alla allo ai alle questo "
          "questa si ha hanno stato essere quando dove tra fra molto suo sua suoi "
          "loro".split(),
}


def _fold(word: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", word)
                   if not unicodedata.combining(c))


_SETS = {code: frozenset(_fold(w.lower()) for w in words)
         for code, words in _STOP.items()}
_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)


def detect(texts, decisive: bool = False) -> str | None:
    """The language of this text, or None when it cannot be called.

    `texts` is any iterable of strings. Give descriptions, not titles: a title
    is mostly names ("Los Angeles Kings at Las Vegas Golden Knights"), and
    names say nothing about the language. Words that turn up in names (los, la,
    de, del, le, di, da, do) are not counted for that reason either.
    """
    counts = {code: 0 for code in _SETS}
    total = 0
    for text in texts:
        if not text:
            continue
        for tok in _TOKEN.findall(_fold(str(text).lower())):
            total += 1
            for code, words in _SETS.items():
                if tok in words:
                    counts[code] += 1
    if total < MIN_WORDS:
        return None
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    (best, hits), (_, second) = ranked[0], ranked[1]
    if hits < MIN_HITS or hits / total < MIN_SHARE:
        return None
    if hits < (DECISIVE if decisive else MARGIN) * second:
        return None
    return best


def effective(override, detected, preferred) -> str:
    """The language a channel counts as: the person's choice, else what was
    detected, else the preferred language (an unknown channel is not held
    against the person)."""
    for code in (override, detected, preferred):
        if code in LANGUAGES:
            return code
    return DEFAULT


def name_of(code: str | None) -> str:
    """English name of a language, for sentences."""
    return LANGUAGES[code][1] if code in LANGUAGES else ""


def from_browser(tag: str | None) -> str:
    """The supported language a browser tag like "es-MX" points at, else English."""
    code = (tag or "").strip().lower()[:2]
    return code if code in LANGUAGES else DEFAULT
