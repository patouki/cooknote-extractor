# Cooknote V4.2 — Application navigateur + moteur d'extraction en ligne

**Version indépendante de démonstration.** Un seul domaine HTTPS sert l'interface et l'API. Depuis Chrome sur Android, on colle une URL Instagram Reel, YouTube, Shorts, TikTok ou un site de recettes ; le serveur essaie les éléments publics accessibles (légende, sous-titres, piste audio via faster-whisper, images vidéo par OCR). L'utilisateur **vérifie et corrige la recette avant de l'enregistrer**.

Le logiciel n'accède pas aux comptes privés, ne contourne pas les protections de connexion/DRM et n'offre pas de garantie que l'hébergement pourra récupérer un Reel précis : Instagram bloque fréquemment les serveurs de cloud. Quand cela échoue, importer un fichier vidéo qu'on est autorisé à utiliser ou coller le texte de la recette.

## Déployer en ligne avec Render (Docker)

**Prévoir un compte Render, un dépôt GitHub et un hébergement disposant d'environ 2 Go de RAM** (l'IA consomme beaucoup de mémoire ; le plan spécifié dans `render.yaml` est payant). Aucun accès au compte Render n'est fourni à ChatGPT par cette archive : la mise en ligne doit être lancée par le propriétaire du compte.

1. Décompresser l'archive et placer **le contenu** du dossier `Cooknote-V4.2-Serveur` à la racine d'un nouveau dépôt GitHub (ne pas publier de `.env`, fichiers secrets ou mots de passe).
2. Dans [Render → Blueprints](https://dashboard.render.com/blueprints), créer un blueprint depuis ce dépôt. Render lit automatiquement `render.yaml` et compile `Dockerfile`.
3. Quand Render demande la variable `COOKNOTE_SITE_PASSWORD`, saisir un mot de passe aléatoire de **12 caractères minimum**. Ne pas utiliser la valeur de `.env.example` ! `COOKNOTE_ENV=production` interdit tout démarrage sans mot de passe suffisant.
4. Attendre la construction et le démarrage, puis ouvrir l'URL HTTPS fournie par Render (`https://...onrender.com`). Chrome demande un identifiant : **`cooknote`** ; mot de passe : la valeur choisie à l'étape 3.
5. Sur ton téléphone Android, appuyer sur **+ → Un lien**, coller par exemple `https://www.instagram.com/reel/DeBvYW5u80z/` et appuyer sur **Extraire la recette automatiquement**.
6. Si Instagram refuse l'accès au serveur, l'écran explique l'échec et propose d'importer le fichier vidéo ou de coller sa transcription. Ce n'est **pas** une extraction automatique garantie pour le Reel donné.

Le serveur HTTPS est fourni par Render, aucun autre domaine n'est nécessaire. Le premier traitement audio peut prendre longtemps, car Whisper télécharge le modèle `tiny` dans le cache. Redémarrages / nouveaux déploiements Render peuvent vider le stockage éphémère et provoquer un nouveau téléchargement. Un serveur de 2 Go de RAM est une estimation et doit être surveillé selon les clips et le trafic.

## Déployer sur son propre serveur VPS

```bash
cp .env.example .env
# IMPORTANT : éditer .env et changer le mot de passe
sudo docker compose up --build -d
```

Le port `8000` est lié **uniquement à 127.0.0.1**, par sécurité : installer un reverse-proxy HTTPS (Caddy, Traefik ou Nginx) sur le même VPS et y proxyfier `127.0.0.1:8000`. **Ne pas exposer directement le port 8000 sur Internet.** Le volume `models` conserve les modèles IA entre redémarrages sur le VPS.

## Tester localement sans Docker

Il faut Python 3.11 recommandé et les commandes `ffmpeg`, `ffprobe`, `tesseract` (avec les langues français / anglais). Dans le dossier du projet :

```bash
python -m pip install -r requirements.txt
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Ouvrir http://127.0.0.1:8000 dans Chrome. Sans `COOKNOTE_ENV=production`, la protection par mot de passe est facultative pour les essais locaux (mais doit être activée en ligne).

## Fonctionnalités

- **Import direct d'URL** : YouTube/Shorts, TikTok, Instagram Reel (si accessibles depuis le serveur) et sites de recettes publics.
- **Transcription de la voix** : moteur faster-whisper sur le serveur, modèle `tiny` par défaut (modifier `COOKNOTE_WHISPER_MODEL` en `base`/`small` si la machine le permet).
- **Analyse des images** : FFmpeg / Tesseract reconnaissent le texte superposé à plusieurs moments de la vidéo.
- **Photos** : OCR sur fichier image envoyé depuis le téléphone.
- **MP4, MOV, WebM et audio** : import alternatif quand un lien social est bloqué.
- **Bibliothèque, planning et courses** : fonctionnent dans le navigateur et sont enregistrés via `localStorage` sur l'appareil.
- **Export JSON** : disponible dans les paramètres pour transférer les recettes.

**Important pour passer de V4.1 à V4.2 :** une nouvelle URL HTTPS a un autre stockage navigateur. Exporter ses recettes de V4.1 puis importer le JSON dans V4.2 si on veut conserver sa bibliothèque. Les comptes et la synchronisation multi-appareils ne sont pas encore proposés.

## Sécurité et protection des données

- Interface et API sur le **même domaine HTTPS**, sans CORS ouvert.
- En production, mot de passe HTTP Basic obligatoire, à utiliser uniquement sous HTTPS ; aucun jeton secret intégré au code HTML.
- Validation des URL pour bloquer les adresses locales/privées, filtrage des liens vidéo vers les plateformes supportées ; cela ne supprime pas tous les risques liés à l'extraction depuis Internet. Réserver cette première version à un **usage privé**.
- Un seul traitement lourd simultané et au plus 12 tentatives par heure par IP vue par l'application (limites locales au processus ; ajouter des limites réseau/pare-feu si nécessaire).
- Fichiers média temporairement stockés puis supprimés après la requête. Les vidéos sont **envoyées au serveur** lorsque l'utilisateur les sélectionne ; elles ne restent donc plus uniquement sur son téléphone. Une vidéo publique peut aussi être téléchargée temporairement par le moteur.
- Jusqu'à 100 Mo de vidéo/audio, 8 Mo pour une image et 12 minutes de média ; un proxy / hébergeur peut imposer des limites et délais plus stricts.
- Le moteur et les extracteurs doivent être mis à jour régulièrement ; yt-dlp peut cesser de fonctionner après une modification des plateformes.
- Pas de protection robuste pour les utilisateurs anonymes ou un trafic public massif : ajouter authentification par utilisateur, file de tâches, stockage/limitation partagés et surveillance pour un produit commercial.

## Tests

```bash
python -m pip install pytest httpx playwright
python -m pytest -q tests/test_server.py
python tests/test_browser.py
```

Le test du navigateur utilise Chromium installé localement et simule la réponse `/api/extract` ; il vérifie l'envoi de l'URL et l'écran de révision. `tests/test_server.py` inclut un test de **vraie reconnaissance OCR** sur le MP4 fourni (`examples/demo-video.mp4`) lorsque FFmpeg et Tesseract sont installés. Les résultats sur un lien Instagram réel, l'installation de Whisper dans Docker et le déploiement Render sont à vérifier après mise en ligne.

## Structure

- `web/index.html` : interface V4.2 adaptée de la V4.1 navigateur.
- `web/server-bridge.js` : import automatisé via l'API, conservation des fonctionnalités locales.
- `server.py` : FastAPI, authentification, protections et endpoints.
- `extractor.py`, `multimodal.py` : analyse des sites/vidéos, sous-titres, Whisper et OCR.
- `Dockerfile`, `render.yaml`, `compose.yaml` : création du serveur en ligne.
- `examples/` : mini-vidéo et photo de recette pour essais.
- `tests/` : tests unitaires et scénario utilisateur mobile.