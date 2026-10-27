"""Recipe titles and seasonings grounded in explicitly observed cooking evidence.

Social account names and platform metadata must never become the saved dish title.
Only seasonings actually mentioned in the caption, audio, subtitles or OCR are added.
"""
from __future__ import annotations

import html
import re
from collections.abc import Iterable

FALLBACK_TITLE = 'Recette à nommer'
DISH_WORDS = (
    r'salades?|poulets?|dindes?|b[œo]ufs?|saumons?|p[aâ]tes?|pasta|spaghettis?|'
    r'lasagnes?|riz|risottos?|ramens?|soupes?|velout[eé]s?|quiches?|tartes?|'
    r'gratins?|pizzas?|burgers?|sandwichs?|wraps?|tacos?|burritos?|'
    r'currys?|tajines?|couscous|chilis?|pa[eë]llas?|omelettes?|'
    r'gnocchis?|pur[eé]es?|sauces?|pestos?|houmous|falafels?|'
    r'g[aâ]teaux?|cakes?|cookies?|brownies?|tiramisu|cr[eê]pes?|'
    r'muffins?|moelleux|galettes?|gaufres?|pancakes?|flans?|'
    r'compotes?|sushis?|quinoas?|lentilles?|ratatouilles?|'
    r'pommes?\s+de\s+terre|tofu|thon|crevettes?|poissons?|l[eé]gumes?'
)
DISH_RE = re.compile(r'\b(?:' + DISH_WORDS + r')\b', re.I)
PROMO_RE = re.compile(
    r'\b(?:abonnez|abonne|follow|like|subscribe|partagez|partage|lien\s+en\s+bio|'
    r'code\s+promo|sponsor|original\s+sound|audio\s+original|instagram|tiktok|'
    r'youtube|reels?|\bprofil\b|\bcompte\b|\bfollowers?\b)\b', re.I)
HANDLE_RE = re.compile(r'(?<!\w)@[a-z0-9_.-]+', re.I)
URL_RE = re.compile(r'https?://\S+', re.I)
HASHTAG_RE = re.compile(r'#[\wÀ-ÿ]+', re.I)
INTRO_RE = re.compile(
    r'\b(?:la\s+)?recette\s+(?:rapide\s+|simple\s+|facile\s+|maison\s+)?(?:de\s+|du\s+|des\s+|d[’\']\s*|:)?'
    r'|\b(?:aujourd[’\']hui\s+)?(?:on\s+|je\s+|nous\s+)?(?:pr[eé]pare|cuisine|fait|fais|r[eé]alise)(?:\s+ensemble)?\s+'
    r'|\b(?:comment\s+faire|how\s+to\s+make|let[’\']s\s+make|making)\s+', re.I)

SEASONING_PATTERNS: tuple[tuple[str, str], ...] = (
    ('Paprika fumé', r'paprika\s+fum[eé]'),
    ('Piment d’Espelette', r'piment\s+d[’\']\s*espelette'),
    ('Herbes de Provence', r'herbes?\s+de\s+provence'),
    ('Ail en poudre', r'ail\s+en\s+poudre'),
    ('Oignon en poudre', r'oignons?\s+en\s+poudre'),
    ('Garam masala', r'garam\s+masala'),
    ('Cinq-épices', r'cinq[\s-]*[eé]pices'),
    ('Quatre-épices', r'quatre[\s-]*[eé]pices'),
    ('Sel', r'sel'),
    ('Poivre', r'poivre'),
    ('Paprika', r'paprika'),
    ('Cumin', r'cumin'),
    ('Curcuma', r'curcuma'),
    ('Curry en poudre', r'curry\s+(?:en\s+)?poudre'),
    ('Curry', r'curry'),
    ('Coriandre', r'coriandre'),
    ('Piment', r'piment'),
    ('Cannelle', r'cannelle'),
    ('Muscade', r'muscade'),
    ('Gingembre', r'gingembre'),
    ('Origan', r'origan'),
    ('Thym', r'thym'),
    ('Romarin', r'romarin'),
    ('Basilic', r'basilic'),
    ('Persil', r'persil'),
    ('Safran', r'safran'),
    ('Cardamome', r'cardamome'),
    ('Clous de girofle', r'clous?\s+de\s+girofle'),
    ('Sumac', r'sumac'),
    ('Za’atar', r'za[’\']?atar'),
)
SEASONING_RX = [(label, re.compile(r'(?<![\wÀ-ÿ])(?:' + rx + r')(?![\wÀ-ÿ])', re.I))
                for label, rx in SEASONING_PATTERNS]


def _title_candidate(value: str) -> str:
    value = html.unescape(str(value or ''))
    value = URL_RE.sub('', value)
    value = HANDLE_RE.sub('', value)
    value = HASHTAG_RE.sub('', value)
    value = re.sub(r'(?i)\s*(?:[|–-]\s*)?(?:instagram|tiktok|youtube)(?:\s+(?:reels?|video))?\s*$', '', value)
    value = re.sub(r'^[^:]{1,45}\bon\s+instagram\s*:\s*', '', value, flags=re.I)
    value = re.sub(r'\s+', ' ', value).strip(' \t\r\n"“”‘’✳🍴🥣👩‍🍳👨‍🍳—-:|•!,.;')
    # Social metadata often prefixes the publisher before a colon, bar or dash.
    for segment in re.split(r'\s+[|—–-]\s+|\s*:\s*', value):
        if DISH_RE.search(segment) and not PROMO_RE.search(segment):
            value = segment.strip()
            break
    if re.match(r'(?i)^(?:ajoutez?|m[eé]langez?|coupez?|versez?|cuisez?|assaisonnez?|salez|poivrez|pr[eé]chauffez|incorporez?|servez?|ensuite|puis)\b', value):
        return ''
    # Spoken instructions are not dish names, even when they mention poulet, riz, etc.
    # Example: « Je vais ajouter 600g de blanc de poulet » must not become the title.
    if re.match(r"(?i)^(?:(?:alors|ensuite|donc)\s+)?(?:j[’']|je\s+|on\s+|nous\s+|vous\s+)(?:(?:vais|va|allons|allez)\s+)?(?:ajout\w*|mett\w*|m[eé]lang\w*|vers\w*|coup\w*|cuir\w*|cuis\w*|assaisonn\w*|d[eé]pos\w*|incorpor\w*|saupoudr\w*)\b", value):
        return ''
    if not value or len(value) < 5:
        return ''
    if PROMO_RE.search(value) or not DISH_RE.search(value):
        return ''
    intro = INTRO_RE.search(value)
    if intro:
        trimmed = value[intro.end():].strip(' :–-,. ')
        if DISH_RE.search(trimmed):
            value = trimmed
    # Trim introductory filler before the first explicit dish term.
    m = DISH_RE.search(value)
    if m and m.start() > 35:
        value = value[m.start():]
    value = re.split(r'(?<=[a-zà-ÿ])\s*[.!?;]\s*', value)[0].strip(' -,:. ')
    value = re.sub(r'(?i)\s+en\s+\d+\s*(?:min(?:utes?)?|h(?:eures?)?)\b.*$', '', value)
    if len(value) > 85:
        value = value[:85].rsplit(' ', 1)[0]
    return value[0].upper() + value[1:] if value else ''


def recipe_title(metadata_title: str = '', *evidence: str) -> str:
    """Prefer an explicit dish name in caption/audio/OCR to a publisher's title.

    Never guesses a dish from a list of ingredients; falls back to an editable title.
    """
    candidates = []
    for text in (*evidence, metadata_title):
        if not isinstance(text, str):
            continue
        # Captions and transcripts can be long; consider only short sentence fragments.
        for line in text.splitlines()[:35]:
            for piece in re.split(r'(?<=[.!?;])\s+', line)[:8]:
                piece = piece.strip()
                if len(piece) > 220:
                    piece = piece[:220]
                title = _title_candidate(piece)
                if title:
                    candidates.append(title)
    return candidates[0] if candidates else FALLBACK_TITLE


def explicit_seasonings(ingredients: Iterable[str], evidence: Iterable[str]) -> list[str]:
    """Append explicitly mentioned spices or herbs, without inventing quantities.

    Existing quantified lines (e.g. '1 c. à café de paprika') remain intact.
    Negated instructions such as 'sans sel' do not introduce the ingredient.
    """
    known = list(ingredients or [])
    fragments = [str(part)[:35_000] for part in evidence if part]
    if not fragments:
        return known
    content = '\n'.join(fragments)
    found_names: list[str] = []
    for name, regex in SEASONING_RX:
        found = False
        for match in regex.finditer(content):
            before = content[max(0, match.start() - 12):match.start()]
            if re.search(r'(?i)\b(?:sans|without|no|ni)\s*$', before) or re.search(r'(?i)\bsans\s+\w+\s+ni\s*$', before):
                continue
            found = True
            break
        if not found:
            continue
        if any(regex.search(str(item)) for item in known):
            continue
        # 'Paprika fumé' implies paprika; don't duplicate the generic ingredient.
        if name in ('Paprika', 'Curry', 'Piment') and any(
            candidate.startswith(name) for candidate in found_names
        ):
            continue
        # Generic word in existing line already precludes an additional alias.
        if any(re.search(r'(?i)\b' + re.escape(name.split()[0]) + r'\b', str(it)) for it in known):
            continue
        found_names.append(name)
    return (known + [f'{name} (quantité à préciser)' for name in found_names])[:80]