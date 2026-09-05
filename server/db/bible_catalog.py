from __future__ import annotations
import re


CANONICAL_BOOKS = [
    (1, "Gen", "Genesis", "OT"),
    (2, "Exod", "Exodus", "OT"),
    (3, "Lev", "Leviticus", "OT"),
    (4, "Num", "Numbers", "OT"),
    (5, "Deut", "Deuteronomy", "OT"),
    (6, "Josh", "Joshua", "OT"),
    (7, "Judg", "Judges", "OT"),
    (8, "Ruth", "Ruth", "OT"),
    (9, "1Sam", "1 Samuel", "OT"),
    (10, "2Sam", "2 Samuel", "OT"),
    (11, "1Kgs", "1 Kings", "OT"),
    (12, "2Kgs", "2 Kings", "OT"),
    (13, "1Chr", "1 Chronicles", "OT"),
    (14, "2Chr", "2 Chronicles", "OT"),
    (15, "Ezra", "Ezra", "OT"),
    (16, "Neh", "Nehemiah", "OT"),
    (17, "Esth", "Esther", "OT"),
    (18, "Job", "Job", "OT"),
    (19, "Ps", "Psalms", "OT"),
    (20, "Prov", "Proverbs", "OT"),
    (21, "Eccl", "Ecclesiastes", "OT"),
    (22, "Song", "Song of Songs", "OT"),
    (23, "Isa", "Isaiah", "OT"),
    (24, "Jer", "Jeremiah", "OT"),
    (25, "Lam", "Lamentations", "OT"),
    (26, "Ezek", "Ezekiel", "OT"),
    (27, "Dan", "Daniel", "OT"),
    (28, "Hos", "Hosea", "OT"),
    (29, "Joel", "Joel", "OT"),
    (30, "Amos", "Amos", "OT"),
    (31, "Obad", "Obadiah", "OT"),
    (32, "Jonah", "Jonah", "OT"),
    (33, "Mic", "Micah", "OT"),
    (34, "Nah", "Nahum", "OT"),
    (35, "Hab", "Habakkuk", "OT"),
    (36, "Zeph", "Zephaniah", "OT"),
    (37, "Hag", "Haggai", "OT"),
    (38, "Zech", "Zechariah", "OT"),
    (39, "Mal", "Malachi", "OT"),
    (40, "Matt", "Matthew", "NT"),
    (41, "Mark", "Mark", "NT"),
    (42, "Luke", "Luke", "NT"),
    (43, "John", "John", "NT"),
    (44, "Acts", "Acts", "NT"),
    (45, "Rom", "Romans", "NT"),
    (46, "1Cor", "1 Corinthians", "NT"),
    (47, "2Cor", "2 Corinthians", "NT"),
    (48, "Gal", "Galatians", "NT"),
    (49, "Eph", "Ephesians", "NT"),
    (50, "Phil", "Philippians", "NT"),
    (51, "Col", "Colossians", "NT"),
    (52, "1Thess", "1 Thessalonians", "NT"),
    (53, "2Thess", "2 Thessalonians", "NT"),
    (54, "1Tim", "1 Timothy", "NT"),
    (55, "2Tim", "2 Timothy", "NT"),
    (56, "Titus", "Titus", "NT"),
    (57, "Phlm", "Philemon", "NT"),
    (58, "Heb", "Hebrews", "NT"),
    (59, "Jas", "James", "NT"),
    (60, "1John", "1 John", "NT"),
    (61, "2John", "2 John", "NT"),
    (62, "3John", "3 John", "NT"),
    (63, "1Pet", "1 Peter", "NT"),
    (64, "2Pet", "2 Peter", "NT"),
    (65, "Jude", "Jude", "NT"),
    (66, "Rev", "Revelation", "NT"),
]


BOOK_ALIAS_MAP = {
    "Genesis": 1,
    "Génesis": 1,
    "Начало": 1,
    "Exodus": 2,
    "Éxodo": 2,
    "Исход": 2,
    "Leviticus": 3,
    "Levítico": 3,
    "Левит": 3,
    "Numbers": 4,
    "Números": 4,
    "Числа": 4,
    "Deuteronomy": 5,
    "Deuteronomio": 5,
    "Второзаконие": 5,
    "Joshua": 6,
    "Josué": 6,
    "Иешуа": 6,
    "Judges": 7,
    "Jueces": 7,
    "Судьи": 7,
    "Ruth": 8,
    "Rut": 8,
    "Руфь": 8,
    "1 Samuel": 9,
    "1 Царств": 9,
    "2 Samuel": 10,
    "2 Царств": 10,
    "1 Kings": 11,
    "1 Reyes": 11,
    "3 Царств": 11,
    "2 Kings": 12,
    "2 Reyes": 12,
    "4 Царств": 12,
    "1 Chronicles": 13,
    "1 Crónicas": 13,
    "1 Летопись": 13,
    "2 Chronicles": 14,
    "2 Crónicas": 14,
    "2 Летопись": 14,
    "Ezra": 15,
    "Esdras": 15,
    "Узайр": 15,
    "Nehemiah": 16,
    "Nehemías": 16,
    "Неемия": 16,
    "Esther": 17,
    "Ester": 17,
    "Есфирь": 17,
    "Job": 18,
    "Аюб": 18,
    "Psalms": 19,
    "Salmos": 19,
    "Забур": 19,
    "Proverbs": 20,
    "Proverbios": 20,
    "Мудрые изречения": 20,
    "Ecclesiastes": 21,
    "Eclesiastés": 21,
    "Размышления": 21,
    "Song of Songs": 22,
    "Cantares": 22,
    "Песнь Сулеймана": 22,
    "Isaiah": 23,
    "Isaías": 23,
    "Исаия": 23,
    "Jeremiah": 24,
    "Jeremías": 24,
    "Иеремия": 24,
    "Lamentations": 25,
    "Lamentaciones": 25,
    "Плач": 25,
    "Ezekiel": 26,
    "Ezequiel": 26,
    "Езекиил": 26,
    "Daniel": 27,
    "Даниял": 27,
    "Hosea": 28,
    "Oseas": 28,
    "Осия": 28,
    "Joel": 29,
    "Иоиль": 29,
    "Amos": 30,
    "Amós": 30,
    "Амос": 30,
    "Obadiah": 31,
    "Abdías": 31,
    "Авдий": 31,
    "Jonah": 32,
    "Jonás": 32,
    "Юнус": 32,
    "Micah": 33,
    "Miqueas": 33,
    "Михей": 33,
    "Nahum": 34,
    "Nahúm": 34,
    "Наум": 34,
    "Habakkuk": 35,
    "Habacuc": 35,
    "Аввакум": 35,
    "Zephaniah": 36,
    "Sofonías": 36,
    "Софония": 36,
    "Haggai": 37,
    "Hageo": 37,
    "Аггей": 37,
    "Zechariah": 38,
    "Zacarías": 38,
    "Закария": 38,
    "Malachi": 39,
    "Malaquías": 39,
    "Малахия": 39,
    "Matthew": 40,
    "Mateo": 40,
    "S. Mateo": 40,
    "Матай": 40,
    "Mark": 41,
    "Marcos": 41,
    "S. Marcos": 41,
    "Марк": 41,
    "Luke": 42,
    "Lucas": 42,
    "S. Lucas": 42,
    "Лука": 42,
    "John": 43,
    "Juan": 43,
    "S.Juan": 43,
    "Иохан": 43,
    "Acts": 44,
    "Hechos": 44,
    "Деяния": 44,
    "Romans": 45,
    "Romanos": 45,
    "Римлянам": 45,
    "1 Corinthians": 46,
    "1 Corintios": 46,
    "1 Коринфянам": 46,
    "2 Corinthians": 47,
    "2 Corintios": 47,
    "2 Коринфянам": 47,
    "Galatians": 48,
    "Gálatas": 48,
    "Галатам": 48,
    "Ephesians": 49,
    "Efesios": 49,
    "Эфесянам": 49,
    "Philippians": 50,
    "Filipenses": 50,
    "Филиппийцам": 50,
    "Colossians": 51,
    "Colosenses": 51,
    "Колоссянам": 51,
    "1 Thessalonians": 52,
    "1 Tesalonicenses": 52,
    "1 Фессалоникийцам": 52,
    "2 Thessalonians": 53,
    "2 Tesalonicenses": 53,
    "2 Фессалоникийцам": 53,
    "1 Timothy": 54,
    "1 Timoteo": 54,
    "1 Тиметею": 54,
    "2 Timothy": 55,
    "2 Timoteo": 55,
    "2 Тиметею": 55,
    "Titus": 56,
    "Tito": 56,
    "Титу": 56,
    "Philemon": 57,
    "Filemón": 57,
    "Филимону": 57,
    "Hebrews": 58,
    "Hebreos": 58,
    "Евреям": 58,
    "James": 59,
    "Santiago": 59,
    "Якуб": 59,
    "1 John": 60,
    "1 Juan": 60,
    "1 Иохана": 60,
    "2 John": 61,
    "2 Juan": 61,
    "2 Иохана": 61,
    "3 John": 62,
    "3 Juan": 62,
    "3 Иохана": 62,
    "1 Peter": 63,
    "1 Pedro": 63,
    "1 Петира": 63,
    "2 Peter": 64,
    "2 Pedro": 64,
    "2 Петира": 64,
    "Jude": 65,
    "Judas": 65,
    "Иуда": 65,
    "Revelation": 66,
    "Apocalipsis": 66,
    "Откровение": 66,
}


BOOK_METADATA = {
    book_id: {"osis_id": osis_id, "canonical_name": canonical_name, "testament": testament}
    for book_id, osis_id, canonical_name, testament in CANONICAL_BOOKS
}


# Book names reach us from three places that all spell them differently: LLM
# verse detection over Spanish speech, the iOS app, and hand-typed lookups. An
# exact dict lookup rejected "Psalm 42:8" — the form nearly everyone actually
# writes — so resolution is folded on case, spacing, and punctuation, with a
# singular/plural fallback.
_EXTRA_BOOK_ALIASES = {
    # Psalms is cited in the singular and abbreviated far more than any other
    # book, in both languages.
    "Ps": 19,
    "Psa": 19,
    "Sal": 19,
}


def _normalize_book_key(book_name: str) -> str:
    """Fold a book name for comparison.

    Case, whitespace, and punctuation are discarded so that "S. Juan",
    "S.Juan", and "s juan" all agree. Accents are kept, because they
    distinguish genuine names such as "Génesis".
    """
    return "".join(ch for ch in book_name.casefold() if ch.isalnum())


_NORMALIZED_BOOK_ALIASES: dict[str, int] = {}
for _alias, _book_id in {**BOOK_ALIAS_MAP, **_EXTRA_BOOK_ALIASES}.items():
    _NORMALIZED_BOOK_ALIASES.setdefault(_normalize_book_key(_alias), _book_id)


def canonicalize_book_name(book_name: str) -> tuple[int, str]:
    book_id = BOOK_ALIAS_MAP.get(book_name)

    if book_id is None and book_name:
        key = _normalize_book_key(book_name)
        book_id = _NORMALIZED_BOOK_ALIASES.get(key)
        if book_id is None and key:
            # "Psalm" for "Psalms", "Salmo" for "Salmos".
            alternate = key[:-1] if key.endswith("s") else key + "s"
            book_id = _NORMALIZED_BOOK_ALIASES.get(alternate)

    if book_id is None:
        raise KeyError(f"Unmapped book name: {book_name}")
    return book_id, BOOK_METADATA[book_id]["canonical_name"]


_REFERENCE_RE = re.compile(r"^(?P<book>.+?)\s+(?P<chapter>\d+):(?P<verse_start>\d+)(?:[-–](?P<verse_end>\d+))?$")


def parse_reference(reference: str) -> dict:
    match = _REFERENCE_RE.match(reference.strip())
    if not match:
        raise ValueError(f"Invalid reference format: {reference}")
    book = match.group("book")
    canonical_book_id, canonical_book_name = canonicalize_book_name(book)
    return {
        "book": canonical_book_name,
        "book_id": canonical_book_id,
        "chapter": int(match.group("chapter")),
        "verse_start": int(match.group("verse_start")),
        "verse_end": int(match.group("verse_end")) if match.group("verse_end") else None,
        "reference": reference.strip(),
    }
