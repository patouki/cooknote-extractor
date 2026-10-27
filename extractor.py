"""Extraction déterministe des recettes. Les champs absents ne sont jamais inventés."""
from __future__ import annotations

import html as html_lib
import ipaddress
import json
import re
import socket
from datetime import timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from recipe_polish import recipe_title, explicit_seasonings

MAX_HTML = 3_000_000
SOCIAL_DOMAINS = ('tiktok.com', 'instagram.com', 'youtube.com', 'youtu.be', 'facebook.com', 'fb.watch')
USER_AGENT = 'Mozilla/5.0 (compatible; CooknoteRecipeImporter/3.0; +http://localhost)'
MEASURE = r'(?:kg|g|gr|grammes?|ml|cl|dl|l|litres?|c\.?\s*à\s*(?:soupe|café)|cuill[eè]res?\s*à\s*(?:soupe|caf[eé])|tasses?|pinc[eé]es?|gousses?|tranches?|bo[iî]tes?|sachets?|verres?|cs|cc)'
QTY = r'(?:\d+(?:[.,/]\d+)?|[½¼¾⅓⅔])'
INGREDIENT_PATTERN = re.compile(rf'^[-•*\s]*(?:{QTY})\s*(?:{MEASURE}\b|[a-zà-ÿ]{{2,}})', re.I)
STEP_VERBS = re.compile(r'^(?:préparer|préchauffer|faire|cuire|chauffer|mettre|couper|mélanger|ajouter|verser|éplucher|émincer|hacher|servir|enfourner|laisser|incorporer|fouetter|battre|déposer|égoutter|assaisonner|répartir|réserver|faire revenir|porter|mix[ée]r|toquer|étaler|parsemer|retirer|ajust[ée]r|mettre au four|peel|heat|mix|bake|add|chop|cook|stir|serve|preheat)\b', re.I)
SECTION_ING = re.compile(r'^(?:🛒\s*|🥣\s*|🧂\s*)?(?:les\s+)?ingr[eé]dients?\s*(?:\(([^)]*)\))?\s*[:：-]?\s*$', re.I)
SECTION_STEPS = re.compile(r'^(?:👩‍🍳\s*|👨‍🍳\s*)?(?:la\s+)?(?:pr[eé]paration|[eé]tapes?|instructions?|recette|m[eé]thode|[eé]laboration)\s*[:：-]?\s*$', re.I)
HASHTAGS = re.compile(r'(?<!\w)#[\wÀ-ÿ]+', re.U)
URL_RE = re.compile(r'https?://\S+', re.I)


class ExtractionError(Exception):
    def __init__(self, message: str, code: str = 'source_indisponible'):
        self.code = code
        super().__init__(message)


def validate_public_url(value: str) -> str:
    """Reject local/private addresses and non-web URLs before fetching remote HTML."""
    value = (value or '').strip()
    if len(value) > 2048:
        raise ExtractionError('Lien trop long.', 'url_invalide')
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ExtractionError('Utilise une URL publique http(s) valide.', 'url_invalide')
    if parsed.port not in (None, 80, 443):
        raise ExtractionError('Le port de ce lien n’est pas autorisé.', 'url_invalide')
    host = parsed.hostname.strip('.').lower()
    if host in ('localhost',) or host.endswith(('.local', '.internal')):
        raise ExtractionError('Les adresses locales sont interdites.', 'url_privee')
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ExtractionError('Le domaine est introuvable.', 'source_indisponible')
    if not addresses:
        raise ExtractionError('Le domaine est introuvable.', 'source_indisponible')
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ExtractionError('Cette adresse est privée ou réservée.', 'url_privee')
    return value


def is_social(url: str) -> bool:
    host = urlparse(url).hostname or ''
    return any(host == root or host.endswith('.' + root) for root in SOCIAL_DOMAINS)


def facebook_video_link(url: str) -> bool:
    """Only recognize Facebook share, reel and video URLs on exact official hosts."""
    host = (urlparse(url).hostname or '').strip('.').lower()
    return host == 'fb.watch' or host == 'facebook.com' or host.endswith('.facebook.com')


def resolve_facebook_video_url(url: str) -> str:
    """Resolve an accessible Facebook share redirect or canonical og:url.

    Facebook sometimes returns HTTP 400 or a login wall to cloud IPs.
    If it does, preserve the original URL for the downstream extractor/browser.
    Never follow a redirect outside official Facebook hosts or to private IPs.
    """
    if not facebook_video_link(url):
        return url
    parsed = urlparse(url)
    # Direct /reel/... and /watch URLs are already canonical. Avoid fetching them twice.
    if parsed.hostname != 'fb.watch' and not parsed.path.startswith('/share/'):
        return url
    current = validate_public_url(url)
    try:
        with requests.Session() as session:
            for _ in range(4):
                parsed = urlparse(current)
                if not facebook_video_link(current):
                    return url
                validate_public_url(current)
                with session.get(current, allow_redirects=False, stream=True,
                                 timeout=(4, 8),
                                 headers={'User-Agent': 'Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36',
                                          'Accept': 'text/html,application/xhtml+xml',
                                          'Accept-Language': 'fr-FR,fr;q=0.9'}) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        from urllib.parse import urljoin
                        next_url = urljoin(current, response.headers.get('Location') or '')
                        if not facebook_video_link(next_url):
                            return url
                        current = validate_public_url(next_url)
                        continue
                    if response.status_code != 200:
                        return url
                    # Only inspect a small public HTML header, not arbitrary media.
                    if 'html' not in response.headers.get('Content-Type', '').lower():
                        return current
                    html_parts = []
                    total = 0
                    for chunk in response.iter_content(chunk_size=16384):
                        total += len(chunk)
                        if total > 200_000:
                            break
                        html_parts.append(chunk)
                    snippet = b''.join(html_parts).decode('utf-8', errors='replace')
                    from bs4 import BeautifulSoup
                    doc = BeautifulSoup(snippet, 'html.parser')
                    for tag in doc.find_all('meta', attrs={'property': 'og:url'}):
                        canonical = tag.get('content', '')
                        if canonical and facebook_video_link(canonical):
                            return validate_public_url(canonical)
                    return current
    except (requests.RequestException, ExtractionError, ValueError, OSError):
        return url
    return url


def get_public_html(url: str, *, session=None) -> tuple[str, str]:
    session = session or requests.Session()
    current = url
    for _ in range(5):
        validate_public_url(current)
        try:
            with session.get(current, stream=True, allow_redirects=False, timeout=(5, 12),
                             headers={'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xhtml+xml'}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    redirected = response.headers.get('Location')
                    if not redirected:
                        raise ExtractionError('Redirection sans destination.')
                    current = urljoin(current, redirected)
                    continue
                if response.status_code in (401, 403):
                    raise ExtractionError('Le site exige une connexion ou refuse l’accès.', 'acces_refuse')
                if response.status_code >= 400:
                    raise ExtractionError(f'Le site répond avec une erreur HTTP {response.status_code}.')
                ctype = response.headers.get('Content-Type', '').lower()
                if 'text/html' not in ctype and 'application/xhtml' not in ctype:
                    raise ExtractionError('Ce lien ne renvoie pas une page HTML.')
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_HTML:
                        raise ExtractionError('Page trop volumineuse pour l’analyse.', 'limite')
                    chunks.append(chunk)
                response.encoding = response.apparent_encoding or 'utf-8'
                return b''.join(chunks).decode(response.encoding, errors='replace'), current
        except requests.RequestException as err:
            raise ExtractionError(f'Connexion au site impossible : {type(err).__name__}.') from err
    raise ExtractionError('Trop de redirections.', 'limite')


def plain(value) -> str:
    if isinstance(value, str):
        return BeautifulSoup(html_lib.unescape(value), 'html.parser').get_text(' ', strip=True)
    if isinstance(value, dict):
        return str(value.get('text') or value.get('name') or value.get('value') or '').strip()
    return ''


def list_of_text(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [v for it in value for v in list_of_text(it)]
    if isinstance(value, dict):
        return [plain(value)] if plain(value) else []
    return [str(value).strip()] if str(value).strip() else []


def find_recipe_object(obj, depth=0):
    if depth > 12 or not obj:
        return None
    if isinstance(obj, list):
        for part in obj:
            found = find_recipe_object(part, depth + 1)
            if found:
                return found
    if isinstance(obj, dict):
        kinds = obj.get('@type', [])
        if isinstance(kinds, str):
            kinds = [kinds]
        if any(str(kind).split('/')[-1].lower() == 'recipe' for kind in kinds):
            return obj
        for key in ('@graph', 'mainEntity', 'mainEntityOfPage', 'itemListElement', 'hasPart'):
            if key in obj:
                found = find_recipe_object(obj[key], depth + 1)
                if found:
                    return found
    return None


def flatten_steps(value) -> list[str]:
    if isinstance(value, str):
        result = [plain(x).strip() for x in re.split(r'\n+|(?<=\.)\s+(?=[A-ZÀ-Ÿ])', value)]
        return [x for x in result if x]
    if isinstance(value, list):
        return [s for v in value for s in flatten_steps(v)]
    if isinstance(value, dict):
        if 'itemListElement' in value:
            return flatten_steps(value['itemListElement'])
        return flatten_steps(value.get('text') or value.get('name') or '')
    return []


def iso_minutes(value) -> int:
    m = re.search(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?', str(value or ''), re.I)
    if m:
        return int(m[1] or 0)*24*60 + int(m[2] or 0)*60 + int(m[3] or 0)
    return 0


def servings(value):
    if isinstance(value, list):
        value = next(iter(value), None)
    if isinstance(value, dict):
        value = value.get('value') or value.get('name')
    m = re.search(r'\b(\d+)\b', str(value or ''))
    if m:
        return min(100, max(1, int(m[1])))
    return 2


def image_url(value, base_url=''):
    if isinstance(value, list):
        value = value[0] if value else ''
    if isinstance(value, dict):
        value = value.get('url') or value.get('contentUrl')
    if not isinstance(value, str) or not value:
        return ''
    result = urljoin(base_url, value)
    return result if urlparse(result).scheme in ('https', 'http') else ''


def text_recipe(content: str, *, fallback_title='', platform='texte') -> dict:
    """Parse *only explicit information* in a caption, OCR result, or pasted recipe."""
    content = html_lib.unescape(content or '').replace('\r', '\n')
    content = re.sub(r'[\u200b-\u200f\ufeff]', '', content)
    content = re.sub(r'(?i)(?<=\S)\s*[|｜]\s*(?=ingr[eé]dients?|pr[eé]paration|[eé]tapes?)', '\n', content)
    lines = [re.sub(r'\s+', ' ', s).strip() for s in content.split('\n')]
    lines = [s for s in lines if s]
    title = fallback_title or 'Recette à compléter'
    for l in lines[:4]:
        cleaned = HASHTAGS.sub('', URL_RE.sub('', l)).strip(' "“”✳🍴🥣👩‍🍳👨‍🍳-:|•')
        if cleaned and len(cleaned) <= 95 and not SECTION_ING.match(cleaned) and not SECTION_STEPS.match(cleaned) and not INGREDIENT_PATTERN.match(cleaned):
            if not re.match(r'^(?:pour \d+|\d+ personnes|abonnez|lien en bio)', cleaned, re.I):
                title = cleaned
                break
    ingredients, steps = [], []
    section = ''
    for line in lines:
        simple = line.strip().lstrip('🥣🛒🧂👩‍🍳👨‍🍳✍️').strip()
        if SECTION_ING.match(simple):
            section = 'ingredients'
            continue
        if SECTION_STEPS.match(simple):
            section = 'steps'
            continue
        if line == title or (len(line) > 400):
            continue
        if not re.search(r'[a-zA-ZÀ-ÿ]', simple) or (len(simple) > 120 and section == 'ingredients'):
            continue
        if simple.lstrip().startswith('#') or re.match(r'^(?:@|http|www\.|abonn|retrouve|lien en bio|follow|like|sponsor|code promo)', simple, re.I):
            continue
        item = re.sub(r'^\s*(?:[-•*–]|\d+[.)])\s*', '', simple).strip()
        if not item:
            continue
        quantity = bool(INGREDIENT_PATTERN.match(simple))
        numbered = bool(re.match(r'^\s*\d+[.)]\s*', simple))
        if section == 'ingredients':
            if quantity or (len(item) < 80 and not STEP_VERBS.match(item) and not numbered):
                ingredients.append(item)
            elif STEP_VERBS.match(item):
                section = 'steps'; steps.append(item)
        elif section == 'steps':
            if len(item) < 420:
                steps.append(item)
        elif quantity and not STEP_VERBS.match(item):
            ingredients.append(item)
        elif STEP_VERBS.match(item) or (numbered and len(item) > 23):
            steps.append(item)
    def unique(items):
        found, result = set(), []
        for item in items:
            key = item.casefold()
            if key not in found:
                found.add(key); result.append(item)
        return result[:80]
    portions = re.search(r'\b(?:pour\s+)?(\d+)\s*(?:personnes?|portions?|parts?|servings?)\b', content, re.I)
    duration = re.search(r'\b(?:pr[eé]paration|cuisson|temps|total)\s*:?\s*(\d+)\s*(?:minutes?|mins?)\b', content, re.I)
    return {'title': title[:120], 'description': content[:350].strip(), 'ingredients': unique(ingredients),
            'steps': unique(steps), 'servings': min(100, int(portions[1])) if portions else 2,
            'minutes': int(duration[1]) if duration else 0, 'image': '', 'source': platform}


def completion(recipe: dict) -> str:
    ing = len(recipe.get('ingredients') or [])
    st = len(recipe.get('steps') or [])
    if ing >= 2 and st >= 1:
        return 'complete'
    if ing or st:
        return 'partial'
    return 'none'


def package(recipe: dict, method: str, url: str = '', note: str = '') -> dict:
    # Also include seasonings explicitly mentioned in preparation instructions,
    # even when absent from the source's separate ingredient list.
    recipe['ingredients'] = explicit_seasonings(
        recipe.get('ingredients') or [],
        [recipe.get('description') or '', *(recipe.get('steps') or [])]
    )
    status = completion(recipe)
    return {'status': status, 'method': method, 'recipe': recipe, 'sourceUrl': url,
            'message': note or ('Recette détectée. Vérifiez les quantités avant de la conserver.' if status == 'complete'
                               else 'Informations incomplètes : vérifiez et complétez la recette.' if status == 'partial'
                               else 'Aucune liste d’ingrédients ou étape fiable trouvée.')}


def from_structured(data: dict, url: str, fallback_title='') -> dict:
    raw_ingredients = data.get('recipeIngredient', [])
    ingredients = list_of_text(raw_ingredients)
    steps = flatten_steps(data.get('recipeInstructions'))
    nutrition = data.get('nutrition') or {}
    if not isinstance(nutrition, dict):
        nutrition = {}
    recipe = {'title': plain(data.get('name')) or fallback_title or 'Nouvelle recette',
              'description': plain(data.get('description'))[:350],
              'ingredients': ingredients, 'steps': steps,
              'servings': servings(data.get('recipeYield')),
              'minutes': iso_minutes(data.get('totalTime')) or (iso_minutes(data.get('prepTime')) + iso_minutes(data.get('cookTime'))),
              'image': image_url(data.get('image'), url),
              'source': urlparse(url).hostname or 'site web'}
    cal = re.search(r'\d+(?:[.,]\d+)?', str(nutrition.get('calories') or ''))
    if cal:
        recipe['calories'] = float(cal[0].replace(',', '.'))
    return recipe


def extract_html(content: str, url: str) -> dict:
    soup = BeautifulSoup(content, 'html.parser')
    page_title = (soup.find('meta', property='og:title') or {}).get('content') or (soup.title.get_text(' ', strip=True) if soup.title else '')
    for script in soup.find_all('script', attrs={'type': re.compile('application/ld\\+json', re.I)}):
        try:
            schema = json.loads(script.string or script.get_text())
            obj = find_recipe_object(schema)
            if obj:
                return package(from_structured(obj, url, page_title), 'schema.org/Recipe', url)
        except (ValueError, TypeError):
            continue
    # Several popular WordPress recipe templates, plus schema.org microdata.
    item = soup.select_one('[itemtype*="schema.org/Recipe"]')
    root = item or soup
    def select_text(css):
        return [node.get('content') or node.get_text(' ', strip=True) for node in root.select(css) if (node.get('content') or node.get_text(' ', strip=True))]
    ings = select_text('[itemprop="recipeIngredient"], .wprm-recipe-ingredient, .tasty-recipes-ingredients li, .mv-create-ingredients li')
    steps = select_text('[itemprop="recipeInstructions"] [itemprop="text"], .wprm-recipe-instruction-text, .tasty-recipes-instructions li, .mv-create-instructions li')
    if not steps:
        steps = select_text('[itemprop="recipeInstructions"]')
    if ings or steps:
        return package({'title': page_title or 'Recette de cuisine', 'description': '',
                        'ingredients': ings, 'steps': steps, 'servings': 2, 'minutes': 0,
                        'image': image_url((soup.find('meta', property='og:image') or {}).get('content'), url),
                        'source': urlparse(url).hostname or 'site web'}, 'HTML de recette', url)
    # Only analyse user-readable text, without menu, scripts or navigation.
    for bad in soup.select('script,style,nav,footer,header,aside,form'):
        bad.decompose()
    main = soup.select_one('article') or soup.select_one('main') or soup.body
    text = (main or soup).get_text('\n', strip=True)[:12000]
    parsed = text_recipe(text, fallback_title=page_title, platform=urlparse(url).hostname or 'site web')
    if completion(parsed) != 'none':
        return package(parsed, 'Texte de la page', url)
    return package(parsed, 'Site sans recette détectable', url,
                   'Aucune recette lisible : ouvrez la page et copiez la recette dans l’onglet Texte.')


def extract_social(url: str) -> dict:
    try:
        import yt_dlp
    except ImportError:
        raise ExtractionError('Le module yt-dlp n’est pas installé. Lancez pip install -r requirements.txt.', 'dependance')
    opts = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'noplaylist': True,
            'socket_timeout': 9, 'retries': 1, 'extractor_retries': 0,
            'ignoreerrors': False, 'cachedir': False, 'playlistend': 1}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as err:
        msg = str(err).lower()
        if any(x in msg for x in ('login', 'sign in', 'cookies', 'private', 'authentication')):
            raise ExtractionError('Publication privée ou authentification exigée par la plateforme. Collez sa légende dans « Texte » ou importez une capture.', 'acces_refuse')
        raise ExtractionError('La plateforme refuse ou ne propose pas les données de cette publication. Essayez l’import de légende ou de capture.', 'acces_refuse')
    if not isinstance(info, dict):
        raise ExtractionError('Aucune donnée publique lisible depuis cette publication.', 'source_indisponible')
    if info.get('entries'):
        info = next((e for e in info['entries'] if isinstance(e, dict)), {})
    platform = urlparse(url).hostname or 'réseau social'
    caption = info.get('description') or info.get('caption') or ''
    # yt-dlp often exposes the publisher as the video title. The dish name must
    # come from cooking evidence, never an account or profile name.
    title = recipe_title(str(info.get('title') or ''), str(caption))
    recipe = text_recipe(str(caption), fallback_title=title, platform=platform)
    recipe['title'] = title
    recipe['ingredients'] = explicit_seasonings(recipe.get('ingredients') or [], [str(caption)])
    if not recipe.get('image'):
        recipe['image'] = image_url(info.get('thumbnail'), url)
    recipe['description'] = str(caption)[:350]
    result = package(recipe, 'Légende publique de la publication', url)
    if result['status'] == 'none':
        result['message'] = 'Publication trouvée, mais ingrédients et étapes absents de la légende. Collez le texte ou importez une capture pour les détecter.'
    elif result['status'] == 'partial':
        result['message'] = 'Légende lue ; recette partielle. Complétez les informations manquantes avant la sauvegarde.'
    return result


def extract_url(url: str) -> dict:
    url = validate_public_url(url)
    if is_social(url):
        return extract_social(url)
    html, final_url = get_public_html(url)
    return extract_html(html, final_url)


def get_public_text(url: str, *, max_bytes: int = 350_000) -> str:
    """Bounded text download with public-IP validation at each HTTP redirect."""
    current = url
    with requests.Session() as session:
        for _ in range(5):
            validate_public_url(current)
            try:
                with session.get(current, stream=True, allow_redirects=False, timeout=(5, 12),
                                 headers={'User-Agent': USER_AGENT}) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        current = urljoin(current, response.headers.get('Location') or '')
                        continue
                    response.raise_for_status()
                    chunks, total = [], 0
                    for chunk in response.iter_content(32768):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ExtractionError('Sous-titres trop volumineux.', 'limite')
                        chunks.append(chunk)
                    return b''.join(chunks).decode('utf-8-sig', errors='replace')
            except requests.RequestException as exc:
                raise ExtractionError('Sous-titres non accessibles.', 'source_indisponible') from exc
    raise ExtractionError('Trop de redirections pour les sous-titres.', 'limite')