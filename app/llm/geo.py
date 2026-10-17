"""Small lookup tables for "can this candidate work from where they are?".

The grader asks the model to *report* where a role may be done and which
nationality it requires; deciding whether the candidate qualifies is a lookup,
done here in code (see `global_grader`). The tables are deliberately modest:
they cover the places remote adverts actually name. A place that is not here is
treated as "does not include the candidate", which errs towards flagging a role
for a human look rather than quietly calling it reachable.
"""

from __future__ import annotations

import re

EU = {
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czech republic", "czechia",
    "denmark", "estonia", "finland", "france", "germany", "greece", "hungary", "ireland",
    "italy", "latvia", "lithuania", "luxembourg", "malta", "netherlands", "poland",
    "portugal", "romania", "slovakia", "slovenia", "spain", "sweden",
}
EEA_EXTRA = {"iceland", "liechtenstein", "norway"}
EUROPE_EXTRA = {
    "united kingdom", "uk", "switzerland", "serbia", "albania", "bosnia and herzegovina",
    "kosovo", "montenegro", "north macedonia", "moldova", "ukraine", "belarus", "turkey",
    "georgia", "armenia", "azerbaijan", "andorra", "monaco", "san marino", "russia",
}
AFRICA = {
    "algeria", "angola", "benin", "botswana", "burkina faso", "burundi", "cameroon",
    "cape verde", "chad", "comoros", "congo", "djibouti", "egypt", "eritrea", "eswatini",
    "ethiopia", "gabon", "gambia", "ghana", "guinea", "ivory coast", "kenya", "lesotho",
    "liberia", "libya", "madagascar", "malawi", "mali", "mauritania", "mauritius",
    "morocco", "mozambique", "namibia", "niger", "nigeria", "rwanda", "senegal",
    "sierra leone", "somalia", "south africa", "south sudan", "sudan", "tanzania", "togo",
    "tunisia", "uganda", "zambia", "zimbabwe",
}
MIDDLE_EAST = {
    "bahrain", "iran", "iraq", "israel", "jordan", "kuwait", "lebanon", "oman", "palestine",
    "qatar", "saudi arabia", "syria", "united arab emirates", "uae", "yemen",
}

# Words that mean "anywhere": a role described with one of these has no country
# restriction.
WORLDWIDE = {
    "worldwide", "global", "globally", "anywhere", "international", "any country",
    "all countries", "anywhere in the world", "world-wide",
}

# Region word -> the countries it covers. A region the candidate's country is not
# in does not include them (and a region not listed here includes no one).
_REGIONS: dict[str, set[str]] = {
    "eu": EU,
    "european union": EU,
    "eea": EU | EEA_EXTRA,
    "europe": EU | EEA_EXTRA | EUROPE_EXTRA,
    "emea": EU | EEA_EXTRA | EUROPE_EXTRA | AFRICA | MIDDLE_EAST,
    "africa": AFRICA,
    "mena": MIDDLE_EAST | {"algeria", "egypt", "libya", "morocco", "tunisia", "mauritania", "sudan"},
    "middle east": MIDDLE_EAST,
}

# A few countries go by more than one name.
_COUNTRY_ALIASES = {
    "uk": "united kingdom", "great britain": "united kingdom", "england": "united kingdom",
    "britain": "united kingdom", "usa": "united states", "us": "united states",
    "u.s.": "united states", "america": "united states", "uae": "united arab emirates",
    "czechia": "czech republic", "holland": "netherlands", "the netherlands": "netherlands",
    "cote d'ivoire": "ivory coast",
}

# Nationality (as adverts write it) -> country.
DEMONYMS = {
    "american": "united states", "argentinian": "argentina", "australian": "australia",
    "austrian": "austria", "belgian": "belgium", "brazilian": "brazil", "british": "united kingdom",
    "bulgarian": "bulgaria", "canadian": "canada", "chinese": "china", "colombian": "colombia",
    "croatian": "croatia", "czech": "czech republic", "danish": "denmark", "dutch": "netherlands",
    "egyptian": "egypt", "estonian": "estonia", "finnish": "finland", "french": "france",
    "german": "germany", "greek": "greece", "hungarian": "hungary", "indian": "india",
    "indonesian": "indonesia", "irish": "ireland", "israeli": "israel", "italian": "italy",
    "japanese": "japan", "kenyan": "kenya", "korean": "south korea", "latvian": "latvia",
    "lithuanian": "lithuania", "mexican": "mexico", "moroccan": "morocco", "nigerian": "nigeria",
    "norwegian": "norway", "pakistani": "pakistan", "polish": "poland", "portuguese": "portugal",
    "romanian": "romania", "russian": "russia", "saudi": "saudi arabia", "serbian": "serbia",
    "singaporean": "singapore", "slovak": "slovakia", "slovenian": "slovenia",
    "south african": "south africa", "spanish": "spain", "swedish": "sweden", "swiss": "switzerland",
    "thai": "thailand", "tunisian": "tunisia", "turkish": "turkey", "ukrainian": "ukraine",
    "emirati": "united arab emirates", "vietnamese": "vietnam", "algerian": "algeria",
    "libyan": "libya", "ghanaian": "ghana", "ethiopian": "ethiopia", "filipino": "philippines",
    "new zealander": "new zealand", "belarusian": "belarus", "kazakh": "kazakhstan",
}


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower().strip(".,;"))


def canonical_country(value: str) -> str:
    """Lower-cased country name with aliases folded ("UK" -> "united kingdom")."""
    name = _norm(value)
    return _COUNTRY_ALIASES.get(name, name)


def country_of(location: str) -> str:
    """The country from a "City, Country" string ("Sousse, Tunisia" -> "tunisia")."""
    parts = [p for p in (location or "").split(",") if p.strip()]
    return canonical_country(parts[-1]) if parts else ""


def places_include(places: list[str], country: str) -> bool:
    """Does a list of places an advert names cover this country?

    `places` come from the model ("EU", "Poland", "worldwide", "UK and Ireland").
    Empty `places` is not evidence of anything and returns False; callers decide
    what an unspecified scope means.
    """
    country = canonical_country(country)
    if not country:
        return False
    for place in places:
        name = _norm(place)
        if not name:
            continue
        if name in WORLDWIDE or any(word in name for word in ("worldwide", "anywhere", "globally")):
            return True
        if canonical_country(name) == country:
            return True
        region = _REGIONS.get(name)
        if region is not None and country in region:
            return True
        # "EU countries", "European time zones": match a region word inside the phrase.
        for key, members in _REGIONS.items():
            if re.search(rf"\b{re.escape(key)}\b", name) and country in members:
                return True
        if re.search(rf"\b{re.escape(country)}\b", name):
            return True
    return False


def nationality_country(value: str) -> str:
    """Country behind a nationality or country word; "" when unknown."""
    name = _norm(value)
    if name in DEMONYMS:
        return DEMONYMS[name]
    folded = canonical_country(name)
    known = set(DEMONYMS.values()) | EU | AFRICA | MIDDLE_EAST | EUROPE_EXTRA | EEA_EXTRA
    return folded if folded in known or folded in {"united states", "canada", "india", "china"} else ""
