# colab-browser-agent (WIP)

Petit projet perso en cours : un agent navigateur autonome qui tourne sur mon PC (Chromium via `browser-use`), avec le modèle Vision hébergé à la demande sur un GPU Google Colab via Ollama et relié en privé par Tailscale.

L'idée c'était de ne pas passer par des API payantes au token et de voir ce qu'on arrive à faire avec des modèles open-source (`qwen3-vl:32b` sur A100 ou `mistral-small3.2:24b` sur L4). Pour tester sur autre chose qu'un simple formulaire, j'ai testé sur un remake d'exercices interactifs type Pix (QCM, champs texte/nombre, simulateurs en iframe, fichiers Calc/Writer à télécharger, modifier et réuploader).

## Comment c'est monté

```
PC local (agent.py + Chromium) <--- Tailscale ---> Google Colab (Ollama GPU)
        |
        +---> downloads/local_kit.py + tools.py + LibreOffice headless
```

- **Colab (`colab/colab_gpu_server.ipynb`)** : lance `tailscaled` (userspace) + `ollama serve`, charge le modèle en VRAM et garde la session active tant que la cellule keep-alive tourne. Une dernière cellule coupe tout (`runtime.unassign()`) quand on a fini.
- **PC (`agent.py`)** : utilise `browser-use 1.0` (`mode="ultrafast"` avec l'arbre d'accessibilité), détecte tout seul l'IP Tailscale du Colab actif, et ouvre un Chromium visible avec profil persistant (`./chrome_profile`).
- **Outils locaux (`downloads/local_kit.py` & `tools.py`)** : lecture/édition `.ods`/`.xlsx`/`.odt`/`.docx`/`.pdf`/`.zip`, métadonnées (`meta.xml`, EXIF), conversion PDF via `soffice --headless`, carte ELA pour les retouches JPEG, et helpers CDP pour les iframes cross-origin.

## Où j'en suis

Ce qui tourne déjà :
- installation/lancement en 1 clic (`LANCER_AGENT.bat` / `lancer_agent.sh`),
- connexion auto à Colab via Tailscale même quand le runtime redémarre,
- boucle interactive dans le terminal (on ouvre le site à la main puis on lance l'agent sur la page),
- QCM, champs texte, simulateurs en iframe cross-origin et manipulation de fichiers de bout en bout.

Ce qui coince encore / à améliorer :
- les modèles 24B/32B quantifiés 4-bit montrent encore leurs limites sur l'analyse visuelle très fine ou les recherches scientifiques un peu longues,
- améliorer la vérification automatique après le clic sur validation quand la page met du temps à changer,
- ajouter quelques pages de test locales pour tester sans dépendre d'un site externe.

## Utilisation rapide

1. **Tailscale** : installer Tailscale sur le PC, générer une auth key **Ephemeral + Reusable** dans la console Tailscale, et la mettre dans les Secrets Colab sous le nom `TAILSCALE_AUTH_KEY`.
2. **Colab** : importer `colab/colab_gpu_server.ipynb`, choisir un GPU **A100** (avec `qwen3-vl:32b`) ou **L4** (avec `mistral-small3.2:24b`), puis faire `Ctrl+F9`.
3. **PC** :
```powershell
git clone https://github.com/Nxesuka/colab-browser-agent.git
cd colab-browser-agent
.\LANCER_AGENT.bat
```
Sous Linux/macOS : `./lancer_agent.sh`.

Quand c'est fini, cocher `ARRETER_MAINTENANT` dans la dernière cellule du notebook Colab et l'exécuter pour couper le GPU.

## Licence

MIT — voir [LICENSE](LICENSE).
