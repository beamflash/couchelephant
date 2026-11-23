"""The channel language detector, and what a channel counts as."""
from app import language

SPANISH = [
    "Fútbol Americano NFL",
    "Semana 12 de la temporada regular 2026. En el último enfrentamiento, Buffalo "
    "Bills venció 28-21 a Kansas City Chiefs, y los dos equipos llegan a este "
    "partido con una racha de victorias.",
    "Noticias Telemundo",
    "Las noticias más importantes del día, con el análisis de los hechos que "
    "marcan la agenda y lo que pasa en el país y en el mundo.",
    "La Rosa de Guadalupe",
    "Una joven descubre que su familia le ha ocultado la verdad sobre su pasado, "
    "pero no se imagina lo que va a pasar cuando por fin se entera de todo.",
]

ENGLISH = [
    "NFL Football",
    "Week 12 of the 2026 regular season. In their last meeting, the Buffalo "
    "Bills beat the Kansas City Chiefs 28-21, and both teams come into this game "
    "with a winning streak.",
    "NBC Nightly News",
    "The top stories of the day, with the analysis of the events that are "
    "shaping the agenda and what is happening in the country and the world.",
    "Law and Order",
    "A young woman finds out that her family has hidden the truth about her past, "
    "but she has no idea what will happen when she finally learns it all.",
]

PORTUGUESE = [
    "Futebol Americano NFL",
    "Semana 12 da temporada regular 2026. No último confronto, o Buffalo Bills "
    "venceu o Kansas City Chiefs por 28-21, e as duas equipes chegam a este "
    "jogo com uma sequência de vitórias.",
    "Jornal Nacional",
    "As notícias mais importantes do dia, com a análise dos fatos que marcam a "
    "agenda e o que acontece no país e no mundo, também com muito mais.",
    "Uma jovem descobre que a sua família não lhe contou a verdade sobre o seu "
    "passado, mas ela não sabe o que vai acontecer quando ela finalmente souber.",
]

FRENCH = [
    "La semaine 12 de la saison régulière. Lors de leur dernière rencontre, les "
    "Bills ont battu les Chiefs 28-21, et les deux équipes arrivent dans ce match "
    "avec une série de victoires, mais ce soir il y a des changements pour tous.",
    "Les nouvelles les plus importantes de la journée, avec une analyse des faits "
    "qui marquent le pays et ce qui se passe dans le monde, avec nous.",
]

GERMAN = [
    "Die zwölfte Woche der Saison. Im letzten Spiel haben die Bills die Chiefs "
    "geschlagen, und beide Mannschaften kommen mit einer Siegesserie in dieses "
    "Spiel, das auch für die Fans von großer Bedeutung ist.",
    "Die wichtigsten Nachrichten des Tages, mit der Analyse der Ereignisse, die "
    "das Land bewegen und was in der Welt passiert, sind nicht zu übersehen.",
]

ITALIAN = [
    "La dodicesima settimana della stagione regolare. Nell'ultimo incontro, i "
    "Bills hanno battuto i Chiefs, e le due squadre arrivano a questa partita con "
    "una serie di vittorie, ma non è facile come sembra.",
    "Le notizie più importanti della giornata, con l'analisi dei fatti che "
    "segnano il paese e quello che succede nel mondo, anche per noi.",
]


def test_telemundo_like_text_is_spanish():
    assert language.detect(SPANISH) == "es"


def test_nbc_like_text_is_english():
    assert language.detect(ENGLISH) == "en"


def test_portuguese_is_not_mistaken_for_spanish():
    assert language.detect(PORTUGUESE) == "pt"


def test_the_other_languages():
    assert language.detect(FRENCH) == "fr"
    assert language.detect(GERMAN) == "de"
    assert language.detect(ITALIAN) == "it"


def test_too_little_text_is_unknown():
    assert language.detect(["Fútbol Americano NFL"]) is None
    assert language.detect([]) is None
    assert language.detect([None, ""]) is None


def test_a_listing_of_names_is_unknown():
    names = ["Kansas City Chiefs", "Buffalo Bills", "Denver Broncos"] * 30
    assert language.detect(names) is None


def test_half_and_half_is_unknown():
    assert language.detect(SPANISH + ENGLISH) is None


def test_accents_do_not_matter():
    plain = [t.replace("ú", "u").replace("é", "e").replace("á", "a")
             .replace("í", "i").replace("ó", "o") for t in SPANISH]
    assert language.detect(plain) == "es"


def test_effective_language_is_override_then_detected_then_preferred():
    assert language.effective("es", "en", "fr") == "es"
    assert language.effective(None, "pt", "en") == "pt"
    assert language.effective(None, None, "fr") == "fr"
    assert language.effective(None, None, "xx") == "en"


def test_the_browser_language_is_matched_on_its_first_two_letters():
    assert language.from_browser("es-MX") == "es"
    assert language.from_browser("PT-br") == "pt"
    assert language.from_browser("ja-JP") == "en"
    assert language.from_browser("") == "en"


# ---- English listings full of Spanish, French and Italian looking names ----

def _en_listing(titles):
    summaries = [
        "The game is live from the stadium, and the home team is looking to win "
        "its fourth straight with a lot of the fans in the building.",
        "Coverage of the matchup between two teams that have met before, with the "
        "analysis of what went wrong in the last one.",
    ] * 8
    return titles * 10 + summaries


def test_english_sports_with_spanish_looking_names_is_not_spanish():
    titles = ["Los Angeles Dodgers at San Diego Padres",
              "Los Angeles Kings at Las Vegas Golden Knights",
              "Las Vegas Raiders at Los Angeles Chargers", "Dodgers Live"]
    assert language.detect(_en_listing(titles)) == "en"


def test_titles_alone_never_make_a_language():
    titles = ["Los Angeles Dodgers at San Diego Padres",
              "Los Angeles Kings at Las Vegas Golden Knights", "Dodgers Live"] * 30
    assert language.detect(titles) != "es"


def test_high_school_college_and_race_names_do_not_confuse_it():
    titles = ["De La Salle at Mater Dei", "Del Mar Classic", "Notre Dame at USC",
              "24 Hours of Le Mans", "Santa Clara at San Jose State", "Da Nang Open"]
    assert language.detect(_en_listing(titles)) == "en"


def test_a_decisive_verdict_needs_a_wider_margin():
    mixed = SPANISH + ENGLISH[:2] + ENGLISH[:2]
    assert language.detect(mixed, decisive=True) is None
