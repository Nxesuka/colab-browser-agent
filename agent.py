#!/usr/bin/env python3
"""
agent.py — Agent navigateur autonome (browser-use) dont le « cerveau » est qwen2.5vl:32b
servi par Ollama sur un GPU NVIDIA L4 (Google Colab), joignable via Tailscale (http://colab-gpu:11434).

Exemples :
    python agent.py --url "https://exemple.org/exercice/42" --objective "Résous l'exercice affiché"
    python agent.py                       # utilise START_URL / TASK_OBJECTIVE du fichier .env
    python agent.py --model qwen2.5vl:7b  # secours rapide (lancer le 7B côté Colab)
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")  # AVANT d'importer tools.py (DOWNLOADS_DIR, LIBREOFFICE_PATH…)
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("BROWSER_USE_LOGGING_LEVEL", "info")

import httpx  # noqa: E402
from browser_use import Agent, Browser, ChatOpenAI  # noqa: E402

from tools import (  # noqa: E402
    ALLOW_ASK_HUMAN,
    DOWNLOADS_DIR,
    find_soffice,
    interactive_tools,
    refresh_available_file_paths,
)

log = logging.getLogger("agent")
LOGS_DIR = BASE_DIR / "logs"


# ---
# Paramètres
# ---
def _env_path(name: str, default: str) -> Path:
    path = Path(os.getenv(name) or default).expanduser()
    return (path if path.is_absolute() else BASE_DIR / path).resolve()


def _env_bool(name: str, default: bool = False) -> bool:
    return (os.getenv(name) or str(default)).strip().lower() in {"1", "true", "yes", "oui"}


@dataclass(slots=True)
class Settings:
    ollama_url: str
    colab_url: str
    model: str
    backend: str
    num_ctx: int
    llm_timeout: int
    reasoning_effort: str | None
    start_url: str
    objective: str
    interactive: bool
    max_steps: int
    max_actions: int
    profile_dir: Path
    chrome_path: str | None
    allowed_domains: list[str]
    confirm_submit: bool
    skip_check: bool
    verbose: bool


def parse_settings(argv: list[str] | None = None) -> Settings:
    p = argparse.ArgumentParser(description="Agent navigateur autonome (browser-use + qwen2.5vl sur Colab L4).")
    p.add_argument("--url", default=os.getenv("START_URL", ""), help="URL de départ (si vide : mode interactif).")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="Ouvre le navigateur et attend vos instructions dans le terminal.")
    p.add_argument("--objective", default=os.getenv("TASK_OBJECTIVE", ""), help="Objectif en langage naturel.")
    p.add_argument("--objective-file", type=Path, help="Fichier texte contenant l'objectif (prioritaire).")
    p.add_argument("--model", default=os.getenv("LLM_MODEL", "qwen3-vl:32b"))
    p.add_argument("--reasoning-effort", default=os.getenv("LLM_REASONING_EFFORT", "low"),
                   help="low | medium | high | off (défaut: low pour accélérer qwen3-vl:32b).")
    p.add_argument("--ollama-url", default=os.getenv("OLLAMA_BASE_URL", "http://colab-gpu:11434"))
    p.add_argument("--backend", choices=("openai", "ollama"), default=os.getenv("LLM_BACKEND", "openai"),
                   help="openai = endpoint OpenAI-compatible /v1 (défaut) ; ollama = API native.")
    p.add_argument("--max-steps", type=int, default=int(os.getenv("MAX_STEPS", "40")))
    p.add_argument("--max-actions", type=int, default=int(os.getenv("MAX_ACTIONS_PER_STEP", "5")))
    p.add_argument("--confirm-submit", action="store_true", default=_env_bool("CONFIRM_SUBMIT"),
                   help="Demander confirmation humaine avant chaque validation définitive.")
    p.add_argument("--skip-check", action="store_true", help="Ne pas vérifier la connexion à Ollama au démarrage.")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    objective = a.objective_file.read_text(encoding="utf-8").strip() if a.objective_file else a.objective.strip()
    start_url = a.url.strip()
    interactive = bool(a.interactive or not start_url)
    effort = (a.reasoning_effort or "").strip().lower()
    return Settings(
        ollama_url=a.ollama_url.rstrip("/"),
        colab_url=(os.getenv("COLAB_NOTEBOOK_URL") or "").strip() or "https://colab.research.google.com/",
        model=a.model,
        backend=a.backend,
        num_ctx=int(os.getenv("LLM_NUM_CTX", "65536")),
        llm_timeout=int(os.getenv("LLM_TIMEOUT", "240")),
        reasoning_effort=None if effort in {"", "off", "none"} else effort,
        start_url=start_url,
        objective=objective or "Résous intégralement l'exercice interactif affiché sur la page actuelle.",
        interactive=interactive,
        max_steps=a.max_steps,
        max_actions=a.max_actions,
        profile_dir=_env_path("CHROME_PROFILE_DIR", "chrome_profile"),
        chrome_path=os.getenv("CHROME_PATH") or None,
        allowed_domains=[d.strip() for d in os.getenv("ALLOWED_DOMAINS", "").split(",") if d.strip()],
        confirm_submit=a.confirm_submit,
        skip_check=a.skip_check,
        verbose=a.verbose,
    )


# ---
# Prompts
# ---
TASK_TEMPLATE = """\
OBJECTIF : <<OBJECTIVE>>
URL DE DÉPART (= ONGLET D'EXERCICE) : <<URL>>

MÉTHODE OBLIGATOIRE — à appliquer à CHAQUE question :

1. OBSERVER LA PAGE AVEC `bu.state()` ET `screenshot()` (ÉTAPE 1 SYSTÉMATIQUE)
   - Appelle immédiatement dans ta 1re cellule :
     `exo_tab = current_tab()['target_id']; bu.state(); screenshot()`
   - `bu.state()` affiche du TEXTE BRUT avec tous les contrôles numérotés `[id]` (boutons, cases à cocher, radios,
     champs texte/nombre/`spinbutton`, listes déroulantes, champs fichier même cachés, et iframes).
   - `screenshot()` te montre tout le contenu visuel (images, schémas, et texte à l'intérieur des simulateurs/iframes).
   - NE PARSE JAMAIS `bu.state()` en Python (c'est du texte à LIRE directement, pas un dict ni du JSON) !
     Lis simplement les numéros `[id]` affichés, puis appelle directement `bu.type(id, "...")` / `bu.click(id)`.

2. L'ONGLET D'EXERCICE (`exo_tab`) EST INTOUCHABLE
   - Ne le ferme JAMAIS et n'y change JAMAIS d'URL (sauf en validant la question). Si un clic accidentel fait changer
     d'URL, reviens immédiatement avec `js('history.back()')`.
   - Recherche web ou lien fourni dans l'énoncé : ouvre un NOUVEL onglet (`new_tab("https://duckduckgo.com/?q=...")` ou
     Wikipédia pour éviter les CAPTCHA Google), lis les sources avec `bu.state()`, ferme cet onglet
     (`close_tab(current_tab()['target_id'])`) puis reviens sur l'exercice (`switch_tab(exo_tab)`).

3. FICHIERS TÉLÉCHARGÉS — UTILISE `import local_kit as lk`
   - RÈGLE ANTI-DEVINETTE ABSOLUE : si l'énoncé mentionne un fichier à télécharger, il est INTERDIT de répondre, de
     cocher des cases ou de valider avant d'avoir téléchargé, ouvert et analysé le fichier !
   - Clique le bouton de téléchargement via `bu.click(id)`, puis dans la cellule suivante :
     `import local_kit as lk; f = lk.latest(); print(f)`
   - Si le fichier n'apparaît pas après 2 tentatives, appelle l'outil `ask_human` pour demander à l'utilisateur de
     cliquer sur Télécharger, puis poursuis.
   - Selon le format :
     • Image (`.jpg`/`.png`/`.webp`) : `print(lk.img_info(f)); show_image(str(f))` — si on cherche des **retouches photo**,
       appelle aussi `show_image(lk.ela(f))` (carte ELA) et `lk.crop_grid(f)` si besoin de zoomer !
     • Tableur (`.ods`/`.xlsx`/`.xls`/`.csv`) : `print(lk.sheet(f, formulas=True))`, `print(lk.stats(f))`.
     • Document (`.odt`/`.docx`/`.pdf`/`.doc`/`.rtf`) : `print(lk.doc(f))`, `print(lk.meta(f))` (métadonnées `meta.xml`/auteur/dates/mots).
     • Archive `.zip` : `print(lk.zip_list(f))` puis `print(lk.unzip(f))`.
     • Taille exacte ou empreinte : `print(lk.info(f))` (attention : **1 Ko = 1000 o** vs **1 Kio = 1024 o** !).
   - Ne calcule JAMAIS de tête (sommes, moyennes, tailles, dates, comptages) : calcule toujours en Python.

4. MODIFIER PUIS RÉUPLOADER UN FICHIER
   - Utilise `lk.edit_cells(f, {"B3": 42, "D10": "=SUM(D2:D9)"})`, `lk.sort_filter(...)`, `lk.replace_doc(f, ancien, nouveau)`,
     `lk.style_doc(...)`, `lk.to_pdf(f)` ou `lk.convert(f, fmt)` : ils créent un nouveau fichier `*_modifie.*` sans écraser l'original.
   - Vérifie le fichier produit, repère `[id] file "..."` dans `bu.state()` (même marqué `(hidden)`), puis dépose-le :
     `cdp('DOM.setFileInputFiles', backendNodeId=id, files=[str(Path(chemin_modifie).resolve())])`

5. RÉPONDRE, VÉRIFIER PUIS VALIDER EN 2 CELLULES SÉPARÉES
   - [WARN] RÈGLE D'OR : NE CLIQUE JAMAIS `Je valide` dans la même cellule que tes réponses !
     • Cellule A : coche tes réponses (`bu.click(id)`), remplis le champ (`bu.type(id, "valeur")`) ou manipule le simulateur.
     • Cellule B : lis le `bu.state()` retourné après la Cellule A ! Vérifie qu'AUCUNE action n'a renvoyé `-> no change`,
       que chaque case voulue affiche `checked` et chaque champ `= "valeur"`, et que sur un simulateur l'état indique
       « Vous pouvez valider » (si ce message est mentionné dans la consigne).
     • Seulement alors, clique `Je valide` (`bu.click(valide_id)`), puis vérifie que la question a changé.
   - Simulateurs / iframes (`--- frame ... ---`) :
     • S'il y a un bouton `Commencer la simulation`, clique-le d'abord.
     • Si `bu.click(id)` ou `bu.type(id, ...)` dans une `--- frame ... ---` renvoie `-> no change`, utilise immédiatement :
       `import local_kit as lk; print(lk.frame_click(cdp, js, id))` (ou `lk.frame_type(cdp, js, id, "...")`, `lk.frame_text(cdp)`, `lk.frame_js(cdp, "...")`)
       et appelle `bu.state()` après pour confirmer le changement !
     • Ne bricole JAMAIS `Target.getTargets()['targets']` ni `frameTree['children']` à la main.
   - Glisser-déposer : `import local_kit as lk; lk.drag(cdp, x1, y1, x2, y2)`.
   - Ne clique jamais « Passer » / « Je passe » sauf si la question est techniquement impossible à traiter.
<<SUBMIT_RULE>>
6. BLOCAGES ET LIMITES
   - CAPTCHA, connexion, double authentification, paiement, blocage après 2 essais : ne contourne JAMAIS ; <<BLOCK_RULE>>

7. FIN
   - Termine uniquement via l'outil `finish(result="...")` (après preuve visuelle/DOM de la validation) : résumé des réponses, sources et fichiers.
"""

EXTRA_SYSTEM_TEMPLATE = """\
RÈGLES IMPÉRATIVES POUR `browser-use 1.0` EN MODE `ultrafast` :
- NE RÉPONDS JAMAIS par du texte seul tant que la tâche n'est pas terminée et validée : une réponse texte sans appel d'outil ARRÊTE immédiatement l'agent ! Appelle toujours `python` (ou `ask_human`), puis `finish` à la fin.
- Utilise TOUJOURS `bu.state()`, `bu.click(id)` et `bu.type(id, text)` en priorité :
  • [WARN] `bu.state()` renvoie une CHAÎNE DE TEXTE (`str`), JAMAIS un `dict` ni du JSON ! Ne fais JAMAIS `state['...']`, `el['id']` ni `json.loads(state)` : lis simplement les `[id]` imprimés et passe l'entier directement à `bu.click(id)` ou `bu.type(id, "...")`.
  • Exemple exact en 2 étapes :
      Cellule 1 : `exo_tab = current_tab()['target_id']; bu.state(); screenshot()`
      Cellule 2 : `bu.type(89, "200")`  (puis vérifie `bu.state()` imprimé)
      Cellule 3 : `bu.click(178)`       (clic sur `Je valide` uniquement après confirmation !)
  • [WARN] N'appelle JAMAIS `bu.click(valide_id)` dans la même cellule que tes réponses, et si un clic dans une `--- frame ... ---` renvoie `-> no change`, utilise `import local_kit as lk; lk.frame_click(cdp, js, id)` / `lk.frame_type(cdp, js, id, "...")` !
  • N'écris PAS de sélecteurs CSS inventés (`input[type='text']`, `button[class*=...]`, `:contains(...)`) ni de `cdp('Accessibility.getFullAXTree')` manuel quand `bu.state()` te donne déjà tous les `[id]` !
  • `bu.click(id)` gère les boutons, liens, checkboxes, radios, « TÉLÉCHARGER », « Je valide », et les iframes same-origin.
  • `bu.type(id, "texte")` gère tous les champs texte, nombres/`spinbutton`, dates, `<textarea>` et `<select>`, et vérifie la valeur saisie.
  • Après chaque cellule contenant `bu.click(...)` ou `bu.type(...)`, `bu.state()` est imprimé automatiquement : lis-le pour confirmer le résultat !
- Pour tous les fichiers locaux (dossier `<<DOWNLOADS>>` | LibreOffice headless : `<<SOFFICE>>`), importe `import local_kit as lk` dans `python` :
  • `lk.latest()` (attend & retourne le Path du dernier téléchargement), `lk.ls()`, `lk.info(f)` (tailles + MD5/SHA256)
  • `lk.img_info(f)` (EXIF/dimensions), `lk.ela(f)` (carte retouches JPEG -> `show_image(...)`), `lk.crop_grid(f)` (zoom grille)
  • `lk.sheet(f, formulas=True)`, `lk.stats(f)`, `lk.sort_filter(...)`, `lk.edit_cells(f, {...})`
  • `lk.doc(f)`, `lk.meta(f)`, `lk.replace_doc(...)`, `lk.style_doc(...)`, `lk.to_pdf(f)`, `lk.convert(f, fmt)`
  • `lk.zip_list(f)`, `lk.unzip(f)`, `lk.make_zip(...)`, `lk.read_text(f)`, `lk.read_csv(f)`, `lk.drag(cdp, x1, y1, x2, y2)`
  • Iframes simulateurs : `lk.frame_click(cdp, js, id)`, `lk.frame_type(cdp, js, id, txt)`, `lk.frame_text(cdp)`, `lk.frame_js(cdp, expr)`
- `finish` et `ask_human` sont des OUTILS SÉPARÉS (appelle-les comme tool calls, JAMAIS dans le code Python de `python` !).
- Garde ta réflexion interne très brève (quelques lignes maximum) avant d'appeler l'outil `python`. Reste concis dans tes sorties `print(...)`.
"""


def build_task(
    s: Settings,
    *,
    current_page_only: bool = False,
    current_url: str | None = None,
    objective_override: str | None = None,
) -> str:
    submit_rule = (
        "   - AVANT chaque validation définitive, appelle ask_human avec la réponse prévue et attends l'accord.\n"
        if s.confirm_submit and ALLOW_ASK_HUMAN else ""
    )
    block_rule = (
        "appelle ask_human pour que l'humain agisse dans la fenêtre, puis reprends."
        if ALLOW_ASK_HUMAN else "termine avec done en décrivant le blocage."
    )
    url_label = current_url or s.start_url or "(onglet actuellement ouvert)"
    task = (
        TASK_TEMPLATE.replace("<<OBJECTIVE>>", objective_override or s.objective)
        .replace("<<URL>>", url_label)
        .replace("<<SUBMIT_RULE>>", submit_rule)
        .replace("<<BLOCK_RULE>>", block_rule)
    )
    if current_page_only:
        prefix = (
            "CONTEXTE : La page d'exercice est DÉJÀ OUVERTE dans l'onglet actif du navigateur"
            + (f" ({current_url})" if current_url else "")
            + ". Ne navigue PAS ailleurs, ne recharge PAS cette page : commence immédiatement à lire l'énoncé affiché.\n\n"
        )
        task = prefix + task
    return task


def build_extra_system_message() -> str:
    soffice = find_soffice()
    return (
        EXTRA_SYSTEM_TEMPLATE.replace("<<DOWNLOADS>>", str(DOWNLOADS_DIR))
        .replace("<<SOFFICE>>", f"disponible ({soffice})" if soffice else "INDISPONIBLE (convert_document échouera)")
    )


# ---
# Compatibilité de versions : on ne passe que les paramètres acceptés par la version installée
# ---
def _params(factory: Any) -> dict[str, inspect.Parameter] | None:
    try:
        params = dict(inspect.signature(factory).parameters)
    except (TypeError, ValueError):
        return None
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return None  # accepte **kwargs : on ne filtre pas
    return params


def _filtered(factory: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    clean = {k: v for k, v in kwargs.items() if v is not None}
    params = _params(factory)
    if params is None:
        return clean
    dropped = sorted(set(clean) - set(params))
    if dropped:
        log.debug("%s : paramètres ignorés par cette version : %s", getattr(factory, "__qualname__", factory), dropped)
    return {k: v for k, v in clean.items() if k in params}


def _construct(factory: Any, **kwargs: Any) -> Any:
    return factory(**_filtered(factory, kwargs))


def _accepts(factory: Any, name: str) -> bool:
    params = _params(factory)
    return params is None or name in params


# ---
# Vérification du cerveau distant
# ---
def _discover_colab_tailscale_urls(port: int = 11434) -> list[str]:
    """Retourne les URLs des nœuds Tailscale en ligne dont le nom commence par 'colab-gpu'."""
    ts_exe = shutil.which("tailscale") or (
        r"C:\Program Files\Tailscale\tailscale.exe"
        if Path(r"C:\Program Files\Tailscale\tailscale.exe").is_file() else None
    )
    if not ts_exe:
        return []
    try:
        proc = subprocess.run([ts_exe, "status", "--json"], capture_output=True, text=True, timeout=5)
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        data = json.loads(proc.stdout)
    except Exception:  # noqa: BLE001
        return []

    urls: list[str] = []
    for peer in (data.get("Peer") or {}).values():
        if not peer.get("Online"):
            continue
        host = (peer.get("HostName") or "").lower()
        dns = (peer.get("DNSName") or "").rstrip(".")
        if not (host.startswith("colab-gpu") or dns.lower().startswith("colab-gpu")):
            continue
        target = next((ip for ip in (peer.get("TailscaleIPs") or ()) if "." in ip), "") or dns
        if target:
            urls.append(f"http://{target}:{port}")
    return urls


async def check_brain(s: Settings) -> None:
    log.info("Vérification d'Ollama sur %s …", s.ollama_url)
    candidates = [s.ollama_url]
    parsed = urlparse(s.ollama_url)
    if (parsed.hostname or "").lower().startswith("colab-gpu"):
        for cand in _discover_colab_tailscale_urls(parsed.port or 11434):
            if cand != s.ollama_url and cand not in candidates:
                candidates.append(cand)

    tags: dict[str, Any] | None = None
    loaded: dict[str, Any] = {}
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=10) as client:
        for url in candidates:
            try:
                r_tags = await client.get(f"{url}/api/tags")
                r_tags.raise_for_status()
                r_ps = await client.get(f"{url}/api/ps")
                r_ps.raise_for_status()
                tags, loaded = r_tags.json(), r_ps.json()
                if url != s.ollama_url:
                    s.ollama_url = url
                    log.info("Nœud Tailscale actif détecté automatiquement : %s", url)
                break
            except httpx.HTTPError as exc:
                last_exc = exc

    if tags is None:
        raise RuntimeError(
            f"[ERREUR] Ollama injoignable sur {s.ollama_url} ({last_exc}).\n"
            "   → Le notebook Colab tourne-t-il (cellules 1-7) ? Tailscale est-il connecté sur ce PC ?\n"
            "   → Testez : ping colab-gpu  puis  curl http://colab-gpu:11434/api/tags"
        ) from last_exc
    names = {m.get("name") for m in tags.get("models", [])} | {m.get("model") for m in tags.get("models", [])}
    if s.model not in names and f"{s.model}:latest" not in names:
        raise RuntimeError(f"[ERREUR] Modèle « {s.model} » absent du serveur. Disponibles : {sorted(n for n in names if n)}")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r_show = await client.post(f"{s.ollama_url}/api/show", json={"name": s.model})
            if r_show.status_code == 200:
                caps = r_show.json().get("capabilities")
                if isinstance(caps, list) and "tools" not in caps:
                    raise RuntimeError(
                        f"[ERREUR] Le modèle « {s.model} » ne supporte pas les appels d'outils (capabilities={caps}).\n"
                        "   → browser-use 1.0 exige un modèle vision + tools : utilisez qwen3-vl:32b, "
                        "mistral-small3.2:24b ou qwen3-vl:8b."
                    )
    except httpx.HTTPError:
        pass
    for m in loaded.get("models", []):
        size, vram = m.get("size", 0), m.get("size_vram", 0)
        pct = 100 * vram / size if size else 0
        log.info("%s chargé : %.1f Gio, %.0f %% en VRAM, contexte %s", m.get("name"), size / 1024**3, pct,
                 m.get("context_length", "?"))
        if pct < 99:
            log.warning("[WARN] Une partie du modèle tourne sur CPU côté Colab : inférence lente (baissez le contexte).")
    log.info("[OK] Cerveau distant opérationnel (%s)", s.model)


async def wait_for_brain(s: Settings) -> bool:
    """Attend automatiquement qu'Ollama réponde (ouvre le notebook Colab si besoin et poll toutes les 5 s)."""
    try:
        await check_brain(s)
        return True
    except RuntimeError:
        pass

    print("\n" + "=" * 80)
    print(f"Serveur GPU Colab non encore joignable sur {s.ollama_url}.")
    print(f"Ouverture de Colab : {s.colab_url}")
    print("   → Dans Colab, appuyez simplement sur Ctrl+F9 (Tout exécuter).")
    print("   → Astuce : mettez l'URL exacte de votre notebook dans COLAB_NOTEBOOK_URL (.env).")
    print("Attente automatique du serveur (Ctrl+C pour quitter)…")
    print("=" * 80)
    try:
        webbrowser.open(s.colab_url)
    except Exception:  # noqa: BLE001
        pass

    prev_level = log.level
    log.setLevel(logging.WARNING)
    try:
        while True:
            try:
                await check_brain(s)
                log.setLevel(prev_level)
                print("[OK] Serveur GPU Colab détecté et prêt !")
                return True
            except RuntimeError:
                print(".", end="", flush=True)
                await asyncio.sleep(5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nAttente annulée.")
        return False
    finally:
        log.setLevel(prev_level)


# ---
# Construction LLM / navigateur / agent
# ---
def build_llm(s: Settings) -> Any:
    # Endpoint OpenAI-compatible d'Ollama. La clé est ignorée par Ollama mais requise par le client.
    # Le contexte (64k) est fixé côté serveur par OLLAMA_CONTEXT_LENGTH.
    kwargs: dict[str, Any] = {
        "model": s.model,
        "base_url": f"{s.ollama_url}/v1",
        "api_key": "ollama",
        "temperature": 0.1,
    }
    if s.reasoning_effort:
        kwargs["reasoning_effort"] = s.reasoning_effort
    return ChatOpenAI(**kwargs)


def build_browser(s: Settings, *, keep_alive: bool = False) -> Any:
    s.profile_dir.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "headless": False,
        "user_data_dir": str(s.profile_dir),
        "keep_alive": keep_alive,
    }
    if s.chrome_path:
        kwargs["executable_path"] = s.chrome_path
    if s.allowed_domains:
        kwargs["allowed_domains"] = s.allowed_domains
    return Browser("chromium", **kwargs)


def build_agent(s: Settings, llm: Any, browser: Any, *, task: str | None = None) -> Agent:
    refresh_available_file_paths()
    return Agent(
        task=task or build_task(s),
        llm=llm,
        browser=browser,
        tools=interactive_tools,
        mode="ultrafast",
        workspace=DOWNLOADS_DIR,
        extend_system_message=build_extra_system_message(),
        max_context_chars=160000,
        highlight_actions=True,
        judge=False,
    )


# ---
# Hooks et cycle de vie
# ---
async def on_step_start(agent: Agent) -> None:
    """Rend uploadables les fichiers créés par les outils pendant l'exécution."""
    paths = list(refresh_available_file_paths())
    if hasattr(agent, "available_file_paths"):
        agent.available_file_paths = paths
    settings = getattr(agent, "settings", None)
    if settings is not None and hasattr(settings, "available_file_paths"):
        settings.available_file_paths = paths


async def on_step_end(agent: Agent) -> None:
    state = getattr(agent, "state", None)
    log.info("— étape %s terminée —", getattr(state, "n_steps", "?"))


async def close_browser(browser: Any) -> None:
    for method in ("stop", "kill", "close"):
        fn = getattr(browser, method, None)
        if not callable(fn):
            continue
        try:
            result = fn()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=20)
        except Exception as exc:  # noqa: BLE001
            log.debug("Fermeture via %s() : %s", method, exc)
    log.info("Navigateur fermé proprement.")


def report(history: Any) -> int:
    status = getattr(history, "status", None)
    steps = getattr(history, "steps", None)
    if steps is None and hasattr(history, "number_of_steps"):
        steps = history.number_of_steps()
    duration = getattr(history, "duration_s", None)
    success = bool(history.is_successful()) if hasattr(history, "is_successful") else (status == "completed")
    done = bool(history.is_done()) if hasattr(history, "is_done") else (status == "completed")
    final = history.final_result() if hasattr(history, "final_result") else getattr(history, "text", None)
    errors = [e for e in (history.errors() if hasattr(history, "errors") else []) if e]
    dur_str = f" | Durée : {duration:.1f}s" if isinstance(duration, (int, float)) else ""
    print("\n" + "=" * 80)
    print(f"Statut : {status or done} | Étapes : {steps if steps is not None else '?'}{dur_str} | Erreurs : {len(errors)}")
    print("Résultat final :\n" + (final or "(aucun — limite d'étapes atteinte ?)"))
    print("=" * 80)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out = LOGS_DIR / f"history_{datetime.now():%Y%m%d_%H%M%S}.json"
    if hasattr(history, "save_to_file"):
        try:
            history.save_to_file(out)
            print(f"Historique : {out}")
        except Exception as exc:  # noqa: BLE001
            log.debug("Sauvegarde de l'historique impossible : %s", exc)
    return 0 if (success or status == "completed") else 2


async def _current_url(browser: Any) -> str | None:
    fn = getattr(browser, "get_current_page_url", None)
    if not callable(fn):
        return None
    try:
        res = fn()
        return str(await res if inspect.isawaitable(res) else res)
    except Exception:  # noqa: BLE001
        return None


async def run_interactive(s: Settings) -> int:
    if not s.skip_check and not await wait_for_brain(s):
        return 0
    llm = build_llm(s)
    browser = build_browser(s, keep_alive=True)
    try:
        if hasattr(browser, "start"):
            await browser.start()
        print("\n" + "=" * 80)
        print("Le navigateur est ouvert ! Va sur le site voulu (et connecte-toi si besoin),")
        print("   puis tape ton instruction ici (ou Entrée = résoudre l'exercice affiché).")
        print("   Commandes : 'model <nom>', 'steps <n>', 'q' pour quitter.")
        print("=" * 80)
        while True:
            raw = (await asyncio.to_thread(input, "\nInstruction > ")).strip()
            low = raw.lower()
            if low in {"q", "quit", "exit"}:
                break
            if low.startswith("model "):
                s.model = raw.split(None, 1)[1].strip()
                llm = build_llm(s)
                print(f"[OK] Modèle actif : {s.model}")
                continue
            if low.startswith("steps "):
                try:
                    s.max_steps = max(1, int(raw.split(None, 1)[1]))
                    print(f"[OK] max_steps = {s.max_steps}")
                except ValueError:
                    print("[WARN] Exemple : steps 40")
                continue

            instruction = raw or s.objective
            if not s.skip_check:
                prev_url = s.ollama_url
                if not await wait_for_brain(s):
                    break
                if s.ollama_url != prev_url:
                    llm = build_llm(s)
            cur_url = await _current_url(browser)
            task = build_task(s, current_page_only=True, current_url=cur_url, objective_override=instruction)
            agent = build_agent(s, llm, browser, task=task)
            run_timeout = max(600, s.llm_timeout * s.max_steps)
            log.info("Exécution sur %s | modèle=%s | max %d étapes", cur_url or "onglet actif", s.model, s.max_steps)
            try:
                history = await agent.run(max_steps=s.max_steps, timeout=run_timeout)
                report(history)
            except (KeyboardInterrupt, asyncio.CancelledError):
                print("\nTâche interrompue. Le navigateur reste ouvert — tapez une nouvelle instruction ou 'q'.")
            except Exception:  # noqa: BLE001
                log.exception("[ERREUR] Erreur pendant la tâche (le navigateur reste ouvert)")
    finally:
        if hasattr(browser, "keep_alive"):
            browser.keep_alive = False
        await close_browser(browser)
    return 0


async def run(s: Settings) -> int:
    if s.interactive:
        return await run_interactive(s)
    if not s.skip_check:
        try:
            await check_brain(s)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
    llm = build_llm(s)
    browser = build_browser(s)
    exit_code = 1
    try:
        agent = build_agent(s, llm, browser)
        run_timeout = max(600, s.llm_timeout * s.max_steps)
        log.info("Démarrage : %s | max %d étapes, %d actions/étape", s.start_url, s.max_steps, s.max_actions)
        history = await agent.run(max_steps=s.max_steps, timeout=run_timeout)
        exit_code = report(history)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.warning("Interruption demandée — arrêt propre…")
        exit_code = 130
    except Exception:  # noqa: BLE001
        log.exception("[ERREUR] Erreur fatale de l'agent")
        exit_code = 1
    finally:
        await close_browser(browser)
    return exit_code


def main() -> None:
    s = parse_settings()
    logging.basicConfig(
        level=logging.DEBUG if s.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        code = asyncio.run(run(s))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
