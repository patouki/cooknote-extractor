# Cooknote Extractor V4.2

Serveur multimodal pour Cooknote Base44 : URLs de recettes, vidéo Instagram/TikTok/YouTube accessibles publiquement, sous-titres, transcription Whisper et OCR.

Exécuter avec Docker : `docker build -t cooknote-extractor .` puis `docker run --rm -e COOKNOTE_ENV=production -e COOKNOTE_SITE_PASSWORD=<mot-de-passe-long> -p 8000:8000 cooknote-extractor`.

`GET /ready` : santé du service. `POST /api/extract` : `{ "url": "https://..." }` (HTTP Basic utilisateur `cooknote`). Voir le README complet lors de l'import des sources.

L'accès aux vidéos privées ou protégées contre l'extraction n'est pas garanti.