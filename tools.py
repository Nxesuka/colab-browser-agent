"""
tools.py — Outils locaux exposés à l'agent browser-use (Controller / Tools).

Capacités (toutes confinées au dossier ./downloads) :
  • Fichiers & archives : lister/attendre les téléchargements, lire/écrire .txt/.csv, zip (inspecter,
    extraire, créer), taille exacte (octets, Ko/Kio, Mo/Mio) et empreintes MD5/SHA1/SHA256, images (EXIF).
  • Tableurs Calc/Excel (.ods/.xlsx/.xls/.csv) : lecture avec coordonnées A1, formules, statistiques,
    tri/filtre, modification de cellules/formules -> nouveau fichier prêt à réuploader.
  • Documents Writer/Word (.odt/.docx/.pdf) : texte, métadonnées internes (meta.xml / docProps),
    remplacement de texte, application de styles.
  • Conversion via LibreOffice headless (soffice --headless --convert-to ...).
  • Exécution Python ciblée et confinée (run_python_on_downloads).

Règle d'or : un outil qui modifie un fichier n'écrase JAMAIS l'original ; il crée <nom>_modifie.<ext>.
"""

import asyncio
import fnmatch
import hashlib
import inspect
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

try:  # browser-use >= 0.7 : « Tools » ; versions antérieures : « Controller »
    from browser_use import Tools
except ImportError:  # pragma: no cover
    from browser_use import Controller as Tools  # type: ignore[no-redef]

try:
    from browser_use import ActionResult
except ImportError:  # pragma: no cover
    from browser_use.agent.views import ActionResult  # type: ignore[no-redef]


# ---
# Configuration
# ---
BASE_DIR = Path(__file__).resolve().parent


def _resolve_dir(value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else BASE_DIR / path).resolve()


DOWNLOADS_DIR: Path = _resolve_dir(os.getenv("DOWNLOADS_DIR", "downloads"))
TMP_DIR: Path = DOWNLOADS_DIR / ".agent_tmp"
LO_PROFILE_DIR: Path = Path(tempfile.gettempdir()) / "browser_agent_lo_profile"
MAX_OUTPUT_CHARS: int = int(os.getenv("TOOL_MAX_CHARS", "6000"))
ALLOW_ASK_HUMAN: bool = os.getenv("ALLOW_ASK_HUMAN", "true").strip().lower() in {"1", "true", "yes", "oui"}
PARTIAL_SUFFIXES = {".crdownload", ".part", ".partial", ".download", ".tmp"}

for _d in (DOWNLOADS_DIR, TMP_DIR):
    _d.mkdir(parents=True, exist_ok=True)

tools = Tools()
controller = tools  # alias : « Controller » dans la terminologie historique de browser-use

# Liste partagée avec l'Agent (paramètre available_file_paths) : autorise l'upload des fichiers produits.
AVAILABLE_FILE_PATHS: list[str] = []


class SandboxError(PermissionError):
    """Tentative d'accès hors du dossier ./downloads."""


# ---
# Helpers génériques
# ---
def _is_inside(path: Path, root: Path = DOWNLOADS_DIR) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


_IGNORED_FILES = {"local_kit.py"}


def _iter_files() -> list[Path]:
    """Fichiers de ./downloads (récursif, hors dossiers techniques cachés et local_kit.py), plus récents d'abord."""
    files: list[Path] = []
    for p in DOWNLOADS_DIR.rglob("*"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(DOWNLOADS_DIR).parts
        if any(part.startswith(".") or part == "__pycache__" for part in rel_parts):
            continue
        if p.name in _IGNORED_FILES:
            continue
        files.append(p)
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def refresh_available_file_paths() -> list[str]:
    """Met à jour (en place) la liste des fichiers uploadables et la renvoie."""
    AVAILABLE_FILE_PATHS[:] = [str(p) for p in _iter_files() if p.suffix.lower() not in PARTIAL_SUFFIXES]
    return AVAILABLE_FILE_PATHS


def _rel(path: Path) -> str:
    try:
        return path.relative_to(DOWNLOADS_DIR).as_posix()
    except ValueError:
        return str(path)


def _safe_path(filename: str, *, must_exist: bool = True) -> Path:
    """Résout un nom (relatif à ./downloads, absolu ou file://) en garantissant le confinement."""
    raw = (filename or "").strip().strip("'\"")
    if not raw:
        raise ValueError("Nom de fichier vide.")
    if raw.lower().startswith("file:"):
        from urllib.parse import unquote, urlparse

        raw = unquote(urlparse(raw).path)
        if re.match(r"^/[A-Za-z]:", raw):
            raw = raw[1:]
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = DOWNLOADS_DIR / candidate
    candidate = candidate.resolve()

    def _by_name() -> Path | None:
        matches = [p for p in _iter_files() if p.name.lower() == candidate.name.lower()]
        return matches[0] if len(matches) == 1 else None

    if not _is_inside(candidate):
        found = _by_name() if must_exist else None
        if found:
            return found
        raise SandboxError(f"Accès refusé : « {filename} » est hors du dossier autorisé {DOWNLOADS_DIR}.")
    if must_exist and not candidate.exists():
        found = _by_name()
        if found:
            return found
        recent = ", ".join(_rel(p) for p in _iter_files()[:10]) or "(dossier vide)"
        raise FileNotFoundError(f"Fichier introuvable : « {filename} ». Fichiers récents : {recent}")
    return candidate


def _output_path(source: Path, output_filename: str | None, *, new_suffix: str | None = None) -> Path:
    if output_filename:
        out = _safe_path(output_filename, must_exist=False)
        if new_suffix and not out.suffix:
            out = out.with_suffix(new_suffix)
    else:
        out = source.with_name(f"{source.stem}_modifie{new_suffix or source.suffix}")
    if out.resolve() == source.resolve():
        raise ValueError("Le fichier de sortie doit différer de l'original (l'original est toujours conservé).")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def _sizes(n: int) -> str:
    return (
        f"{n} octets | {n / 1000:.3f} Ko | {n / 1024:.3f} Kio | "
        f"{n / 1000**2:.4f} Mo | {n / 1024**2:.4f} Mio | {n / 1000**3:.6f} Go"
    )


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


_AR_FIELDS: set[str] = set(getattr(ActionResult, "model_fields", {}) or {})


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n… [TRONQUÉ : {len(text) - limit} caractères de plus — affinez (start_char, max_rows, sheet, columns…)]"
    )


def _ok(text: str, memory: str | None = None) -> ActionResult:
    text = _truncate(text)
    kwargs: dict[str, Any] = {"extracted_content": text}
    if "include_in_memory" in _AR_FIELDS:
        kwargs["include_in_memory"] = True
    # Contexte de 16k tokens : les sorties longues ne sont montrées qu'une fois, un résumé reste en mémoire.
    if len(text) > 1500 and {"long_term_memory", "include_extracted_content_only_once"} <= _AR_FIELDS:
        kwargs["long_term_memory"] = memory or (text[:600] + " …")
        kwargs["include_extracted_content_only_once"] = True
    return ActionResult(**kwargs)


def _err(text: str) -> ActionResult:
    return ActionResult(error=_truncate(text, 2500))


async def _run(fn: Callable[..., "str | ActionResult"], *args: Any, **kwargs: Any) -> ActionResult:
    """Exécute l'implémentation synchrone dans un thread et convertit exceptions -> ActionResult(error)."""
    try:
        out = await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — l'agent doit voir l'erreur, pas planter
        out = _err(f"{type(exc).__name__} : {exc}")
    refresh_available_file_paths()
    return out if isinstance(out, ActionResult) else _ok(str(out))


def agent_action(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Enregistre `fn` comme action browser-use : sa docstring devient la description lue par le LLM."""
    return tools.action(inspect.cleandoc(fn.__doc__ or fn.__name__))(fn)


def _read_text(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def _text_stats(text: str) -> str:
    words = len(re.findall(r"\S+", text))
    no_space = len(re.sub(r"\s", "", text))
    lines = text.count("\n") + 1 if text else 0
    return (
        f"{words} mots | {len(text)} caractères (espaces compris) | "
        f"{no_space} caractères (hors espaces) | {lines} lignes"
    )


# ---
# LibreOffice headless
# ---
_SOFFICE_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def find_soffice() -> str | None:
    """Localise l'exécutable LibreOffice (variables LIBREOFFICE_PATH/SOFFICE_PATH, PATH, emplacements standards)."""
    candidates: list[str] = [os.environ[v] for v in ("LIBREOFFICE_PATH", "SOFFICE_PATH") if os.getenv(v)]
    system = platform.system()
    if system == "Windows":
        names = ("soffice.com", "soffice.exe", "soffice")
        bases = [os.getenv("ProgramFiles"), os.getenv("ProgramW6432"), os.getenv("ProgramFiles(x86)")]
        if os.getenv("LOCALAPPDATA"):
            bases.append(str(Path(os.environ["LOCALAPPDATA"]) / "Programs"))
        standard = [
            str(Path(b) / "LibreOffice" / "program" / exe)
            for b in bases if b for exe in ("soffice.com", "soffice.exe")
        ]
    elif system == "Darwin":
        names = ("soffice",)
        standard = ["/Applications/LibreOffice.app/Contents/MacOS/soffice"]
    else:
        names = ("soffice", "libreoffice")
        standard = [
            "/usr/bin/soffice", "/usr/bin/libreoffice", "/usr/lib/libreoffice/program/soffice",
            "/snap/bin/libreoffice", "/var/lib/flatpak/exports/bin/org.libreoffice.LibreOffice",
            *sorted(str(p) for p in Path("/opt").glob("libreoffice*/program/soffice")),
        ]
    candidates += [w for n in names if (w := shutil.which(n))]
    candidates += standard
    for cand in candidates:
        if cand and Path(cand).is_file():
            return str(Path(cand))
    return None


def _soffice_convert(src: Path, target: str, outdir: Path, timeout_s: int = 180) -> Path:
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError(
            "LibreOffice introuvable. Installez-le ou définissez LIBREOFFICE_PATH "
            "(ex. C:\\Program Files\\LibreOffice\\program\\soffice.exe ou /usr/bin/soffice)."
        )
    outdir.mkdir(parents=True, exist_ok=True)
    ext = target.split(":")[0].lower()
    expected = outdir / f"{src.stem}.{ext}"
    if expected.exists() and expected.resolve() != src.resolve():
        expected.unlink()
    cmd = [
        soffice, f"-env:UserInstallation={LO_PROFILE_DIR.as_uri()}",
        "--headless", "--invisible", "--nologo", "--norestore", "--nolockcheck", "--nodefault",
        "--convert-to", target, "--outdir", str(outdir), str(src),
    ]
    with _SOFFICE_LOCK:  # un profil LibreOffice ne supporte qu'une instance à la fois
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        deadline = time.time() + 20  # soffice.exe peut rendre la main avant la fin de l'écriture
        last_size = -1
        while time.time() < deadline:
            if expected.exists():
                size = expected.stat().st_size
                if size > 0 and size == last_size:
                    break
                last_size = size
            time.sleep(0.5)
    if not expected.exists():
        raise RuntimeError(f"Conversion échouée ({proc.returncode}) : {proc.stdout.strip()} {proc.stderr.strip()}")
    return expected


# ---
# Implémentations : fichiers, archives, images
# ---
def _list_downloads(limit: int, pattern: str) -> str:
    files = [p for p in _iter_files() if fnmatch.fnmatch(p.name.lower(), (pattern or "*").lower())]
    if not files:
        return f"Aucun fichier correspondant à « {pattern} » dans {DOWNLOADS_DIR}."
    lines = [f"Dossier : {DOWNLOADS_DIR} — {len(files)} fichier(s), du plus récent au plus ancien :"]
    for i, p in enumerate(files[: max(1, limit)], 1):
        flag = "  TÉLÉCHARGEMENT EN COURS" if p.suffix.lower() in PARTIAL_SUFFIXES else ""
        lines.append(f"{i}. {p} | {p.stat().st_size} octets | {_mtime(p)}{flag}")
    return "\n".join(lines)


def _wait_for_download(timeout_s: int, since_seconds: int) -> str:
    start = time.time()
    threshold = start - max(0, since_seconds)
    deadline = start + max(1, min(timeout_s, 300))
    sizes: dict[Path, int] = {}
    while True:
        files = _iter_files()
        partial = [p for p in files if p.suffix.lower() in PARTIAL_SUFFIXES]
        recent = [p for p in files if p.suffix.lower() not in PARTIAL_SUFFIXES and p.stat().st_mtime >= threshold]
        stable = recent and all(sizes.get(p) == p.stat().st_size for p in recent)
        if recent and not partial and stable:
            lines = ["[OK] Téléchargement(s) terminé(s) :"]
            lines += [f"- {p} | {_sizes(p.stat().st_size)}" for p in recent]
            return "\n".join(lines)
        sizes = {p: p.stat().st_size for p in recent}
        if time.time() > deadline:
            msg = f"Aucun téléchargement terminé dans les {since_seconds}s précédentes + {timeout_s}s d'attente."
            if partial:
                msg += " En cours : " + ", ".join(p.name for p in partial)
            return msg + "\n" + _list_downloads(5, "*")
        time.sleep(1)


def _read_text_file(filename: str, start_char: int, max_chars: int) -> str:
    path = _safe_path(filename)
    text, enc = _read_text(path)
    start = max(0, start_char)
    chunk = text[start : start + max(100, max_chars)]
    header = f"Fichier : {path} | encodage {enc} | {_text_stats(text)} | extrait [{start}:{start + len(chunk)}]"
    return f"{header}\n-----\n{chunk}"


def _write_text_file(filename: str, content: str, append: bool) -> str:
    path = _safe_path(filename, must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"[OK] Écrit : {path} | {_sizes(path.stat().st_size)} | SHA256 {digest}"


def _read_csv_file(filename: str, max_rows: int, separator: str) -> str:
    import io

    import pandas as pd

    path = _safe_path(filename)
    text, enc = _read_text(path)
    sep = None if separator in ("", "auto") else separator
    df = pd.read_csv(io.StringIO(text), sep=sep, engine="python")
    out = [
        f"Fichier : {path} | encodage {enc} | {df.shape[0]} lignes x {df.shape[1]} colonnes",
        "Colonnes : " + ", ".join(f"{c} ({t})" for c, t in df.dtypes.astype(str).items()),
        "--- Aperçu ---",
        df.head(max(1, max_rows)).to_string(),
    ]
    num = df.select_dtypes("number")
    if not num.empty:
        out += ["--- Statistiques numériques ---", num.describe().T.to_string()]
    return "\n".join(out)


def _file_info_and_hash(filename: str) -> str:
    path = _safe_path(filename)
    hashes = {name: hashlib.new(name) for name in ("md5", "sha1", "sha256")}
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            for h in hashes.values():
                h.update(block)
    st = path.stat()
    mime = mimetypes.guess_type(path.name)[0] or "inconnu"
    return "\n".join([
        f"Fichier : {path}",
        f"Taille : {_sizes(st.st_size)}",
        "(1 Ko = 1000 octets ; 1 Kio = 1024 octets ; 1 Mo = 1 000 000 ; 1 Mio = 1 048 576)",
        f"Type MIME : {mime} | extension : {path.suffix or '(aucune)'}",
        f"Modifié : {_mtime(path)} | créé : {datetime.fromtimestamp(st.st_ctime):%Y-%m-%d %H:%M:%S}",
        *(f"{name.upper()} : {h.hexdigest()}" for name, h in hashes.items()),
    ])


def _zip_name(info: zipfile.ZipInfo) -> str:
    """Corrige les noms non-UTF-8 (archives Windows en cp437/cp850)."""
    if info.flag_bits & 0x800:
        return info.filename
    for enc in ("utf-8", "cp850"):
        try:
            return info.filename.encode("cp437").decode(enc)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return info.filename


def _inspect_zip(filename: str) -> str:
    path = _safe_path(filename)
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        files = [i for i in infos if not i.is_dir()]
        total = sum(i.file_size for i in files)
        comp = sum(i.compress_size for i in files)
        lines = [
            f"Archive : {path} | {len(files)} fichier(s), {len(infos) - len(files)} dossier(s)",
            f"Taille décompressée totale : {_sizes(total)}",
            f"Taille compressée : {comp} octets | ratio {100 * comp / total:.1f} %" if total else "Archive vide",
        ]
        if zf.comment:
            lines.append(f"Commentaire : {zf.comment.decode('utf-8', 'replace')}")
        lines.append("--- Contenu (nom | taille | compressé | date | CRC32 | chiffré) ---")
        for i in infos[:200]:
            enc = "oui" if i.flag_bits & 0x1 else "non"
            date = "%04d-%02d-%02d %02d:%02d:%02d" % i.date_time
            lines.append(f"{_zip_name(i)} | {i.file_size} | {i.compress_size} | {date} | {i.CRC:08x} | {enc}")
        if len(infos) > 200:
            lines.append(f"… {len(infos) - 200} entrées supplémentaires")
    return "\n".join(lines)


def _extract_zip(filename: str, destination_folder: str | None, password: str | None) -> str:
    path = _safe_path(filename)
    dest = _safe_path(destination_folder, must_exist=False) if destination_folder else path.with_name(path.stem)
    dest.mkdir(parents=True, exist_ok=True)
    pwd = password.encode() if password else None
    extracted: list[Path] = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            target = (dest / _zip_name(info)).resolve()
            if not _is_inside(target, dest):
                raise SandboxError(f"Entrée dangereuse (zip-slip) refusée : {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, pwd=pwd) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(target)
    lines = [f"[OK] {len(extracted)} fichier(s) extrait(s) dans {dest} :"]
    lines += [f"- {p} ({p.stat().st_size} octets)" for p in extracted[:100]]
    return "\n".join(lines)


def _create_zip(output_filename: str, files: str) -> str:
    out = _safe_path(output_filename, must_exist=False)
    if out.suffix.lower() != ".zip":
        out = out.with_suffix(".zip")
    patterns = [f.strip() for f in files.split(",") if f.strip()]
    selected: list[Path] = []
    for pat in patterns:
        if any(c in pat for c in "*?["):
            selected += [p for p in _iter_files() if fnmatch.fnmatch(_rel(p).lower(), pat.lower())]
        else:
            p = _safe_path(pat)
            selected += [f for f in p.rglob("*") if f.is_file()] if p.is_dir() else [p]
    selected = [p for p in dict.fromkeys(selected) if p.resolve() != out.resolve()]
    if not selected:
        raise FileNotFoundError("Aucun fichier sélectionné pour l'archive.")
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in selected:
            zf.write(p, arcname=_rel(p))
    return f"[OK] Archive créée : {out} | {len(selected)} fichier(s) | {_sizes(out.stat().st_size)}"


def _inspect_image(filename: str) -> str:
    from PIL import ExifTags, Image

    path = _safe_path(filename)
    with Image.open(path) as img:
        lines = [
            f"Image : {path}",
            f"Format : {img.format} | dimensions : {img.width} x {img.height} px | mode : {img.mode}",
            f"Images (frames) : {getattr(img, 'n_frames', 1)} | DPI : {img.info.get('dpi', 'non renseigné')}",
            f"Taille fichier : {_sizes(path.stat().st_size)}",
        ]
        exif = img.getexif()
        if exif:
            lines.append("--- EXIF ---")
            for tag_id, value in exif.items():
                lines.append(f"{ExifTags.TAGS.get(tag_id, tag_id)} : {value}")
            for ifd_id, names in ((0x8769, ExifTags.TAGS), (0x8825, ExifTags.GPSTAGS)):
                try:
                    for tag_id, value in exif.get_ifd(ifd_id).items():
                        lines.append(f"{names.get(tag_id, tag_id)} : {value}")
                except Exception:  # noqa: BLE001
                    pass
        else:
            lines.append("Aucune donnée EXIF.")
        for key in ("Description", "Comment", "Author", "Software", "Title"):
            if key in img.info:
                lines.append(f"{key} : {img.info[key]}")
    return "\n".join(lines)


# ---
# Implémentations : tableurs
# ---
_ENGINES = {".ods": "odf", ".xlsx": "openpyxl", ".xlsm": "openpyxl", ".xls": "xlrd"}


def _engine(path: Path) -> str:
    try:
        return _ENGINES[path.suffix.lower()]
    except KeyError as exc:
        raise ValueError(f"Format tableur non supporté : {path.suffix} (attendu .ods/.xlsx/.xlsm/.xls/.csv)") from exc


def _load_sheets(path: Path, header_row: int) -> dict[str, Any]:
    import io

    import pandas as pd

    header = header_row - 1 if header_row > 0 else None
    if path.suffix.lower() in (".csv", ".tsv", ".txt"):
        text, _ = _read_text(path)
        return {"csv": pd.read_csv(io.StringIO(text), sep=None, engine="python", header=header)}
    return pd.read_excel(path, sheet_name=None, header=header, engine=_engine(path))


def _pick_sheet(sheets: dict[str, Any], sheet: str | None) -> str:
    if not sheet:
        return next(iter(sheets))
    for name in sheets:
        if name.lower() == sheet.strip().lower():
            return name
    raise KeyError(f"Feuille « {sheet} » introuvable. Feuilles : {list(sheets)}")


def _list_formulas(path: Path, sheet: str | None, limit: int = 80) -> list[str]:
    from openpyxl.utils import get_column_letter

    found: list[str] = []
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(path, data_only=False, read_only=True)
        for ws in wb.worksheets:
            if sheet and ws.title.lower() != sheet.lower():
                continue
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        found.append(f"{ws.title}!{cell.coordinate} : {cell.value}")
        wb.close()
    elif ext == ".ods":
        from odf import teletype
        from odf.namespaces import OFFICENS, TABLENS
        from odf.opendocument import load
        from odf.table import Table, TableRow

        doc = load(str(path))
        for table in doc.spreadsheet.getElementsByType(Table):
            name = table.getAttrNS(TABLENS, "name")
            if sheet and name.lower() != sheet.lower():
                continue
            r = 0
            for row in table.getElementsByType(TableRow):
                c = 0
                for cell in row.childNodes:
                    qname = getattr(cell, "qname", (None, None))
                    if qname[1] not in ("table-cell", "covered-table-cell"):
                        continue
                    formula = cell.getAttrNS(TABLENS, "formula")
                    if formula:
                        value = cell.getAttrNS(OFFICENS, "value") or teletype.extractText(cell)
                        found.append(f"{name}!{get_column_letter(c + 1)}{r + 1} : {formula} (valeur = {value})")
                    c += int(cell.getAttrNS(TABLENS, "number-columns-repeated") or 1)
                r += int(row.getAttrNS(TABLENS, "number-rows-repeated") or 1)
    return found[:limit] if found else ["(aucune formule)"]


def _read_spreadsheet(filename: str, sheet: str | None, max_rows: int, show_formulas: bool) -> str:
    from openpyxl.utils import get_column_letter

    path = _safe_path(filename)
    sheets = _load_sheets(path, header_row=0)  # grille brute : coordonnées A1 exactes
    targets = [_pick_sheet(sheets, sheet)] if sheet else list(sheets)[:3]
    out = [f"Fichier : {path} | feuilles : {list(sheets)}",
           "Colonnes = lettres, lignes = numéros réels (référence A1 utilisable dans set_spreadsheet_cells)."]
    for name in targets:
        df = sheets[name].copy()
        df.columns = [get_column_letter(i + 1) for i in range(df.shape[1])]
        df.index = range(1, len(df) + 1)
        used = df.dropna(how="all").dropna(axis=1, how="all")
        out.append(f"--- Feuille « {name} » : {used.shape[0]} lignes x {used.shape[1]} colonnes non vides ---")
        out.append(used.head(max(1, max_rows)).to_string(na_rep="") if not used.empty else "(vide)")
        if used.shape[0] > max_rows:
            out.append(f"… {used.shape[0] - max_rows} lignes non affichées (augmentez max_rows)")
    if len(sheets) > len(targets):
        out.append(f"(Autres feuilles non affichées : précisez sheet=…)")
    if show_formulas and path.suffix.lower() in (".ods", ".xlsx", ".xlsm"):
        out += ["--- Formules ---", *_list_formulas(path, sheet)]
    return "\n".join(out)


def _numeric(series: Any) -> Any:
    import pandas as pd

    if pd.api.types.is_numeric_dtype(series):
        return series
    cleaned = series.astype(str).str.replace("\u00a0", "", regex=False).str.replace(" ", "", regex=False)
    cleaned = cleaned.str.replace("€", "", regex=False).str.replace("%", "", regex=False).str.replace(",", ".", regex=False)
    converted = pd.to_numeric(cleaned, errors="coerce")
    non_empty = series.notna().sum()
    return converted if non_empty and converted.notna().sum() / non_empty >= 0.8 else None


def _spreadsheet_stats(filename: str, sheet: str | None, columns: str | None, header_row: int) -> str:
    path = _safe_path(filename)
    sheets = _load_sheets(path, header_row)
    name = _pick_sheet(sheets, sheet)
    df = sheets[name].dropna(how="all")
    cols = [c.strip() for c in columns.split(",")] if columns else list(df.columns)
    lookup = {str(c).strip().lower(): c for c in df.columns}
    missing = [c for c in cols if str(c).strip().lower() not in lookup]
    if missing:
        raise KeyError(f"Colonnes introuvables : {missing}. Disponibles : {[str(c) for c in df.columns]}")
    out = [f"Fichier : {path} | feuille « {name} » | {len(df)} lignes de données (en-tête ligne {header_row})"]
    for c in cols:
        col = df[lookup[str(c).strip().lower()]]
        num = _numeric(col)
        if num is not None and num.notna().any():
            s = num.dropna()
            out.append(
                f"[{c}] numérique : n={len(s)} | somme={s.sum():.6g} | moyenne={s.mean():.6g} | "
                f"médiane={s.median():.6g} | min={s.min():.6g} | max={s.max():.6g} | écart-type={s.std():.6g}"
            )
        else:
            vc = col.dropna().astype(str).value_counts()
            top = ", ".join(f"{k} ({v})" for k, v in vc.head(8).items())
            out.append(f"[{c}] texte : n={int(col.notna().sum())} | valeurs distinctes={len(vc)} | top : {top}")
    return "\n".join(out)


def _write_sheets(out: Path, sheets: dict[str, Any], header_sheet: str | None) -> None:
    import pandas as pd

    if out.suffix.lower() in (".csv", ".tsv", ".txt"):
        next(iter(sheets.values())).to_csv(out, index=False, encoding="utf-8")
        return
    engine = {".ods": "odf", ".xlsx": "openpyxl", ".xlsm": "openpyxl"}.get(out.suffix.lower())
    if not engine:
        raise ValueError(f"Écriture non supportée pour {out.suffix} (utilisez .ods, .xlsx ou .csv)")
    with pd.ExcelWriter(out, engine=engine) as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=str(name)[:31], index=False, header=(name == header_sheet))


def _sort_filter_spreadsheet(filename: str, sheet: str | None, sort_by: str | None, ascending: bool,
                             filter_query: str | None, header_row: int, output_filename: str | None) -> str:
    path = _safe_path(filename)
    sheets = _load_sheets(path, header_row)
    name = _pick_sheet(sheets, sheet)
    df = sheets[name].dropna(how="all")
    before = len(df)
    if filter_query:
        df = df.query(filter_query, engine="python")
    if sort_by:
        keys = [k.strip() for k in sort_by.split(",") if k.strip()]
        df = df.sort_values(by=keys, ascending=ascending, kind="mergesort")
    raw = _load_sheets(path, header_row=0) if path.suffix.lower() not in (".csv", ".tsv", ".txt") else {}
    raw[name] = df
    out = _output_path(path, output_filename)
    _write_sheets(out, raw if raw else {name: df}, header_sheet=name if header_row > 0 else None)
    return "\n".join([
        f"[OK] Nouveau fichier : {out} ({before} -> {len(df)} lignes ; mise en forme non conservée)",
        df.head(15).to_string(),
    ])


_NUM_RE = re.compile(r"^-?(0|[1-9]\d*)([.,]\d+)?$")


def _coerce(value: Any) -> Any:
    if isinstance(value, str):
        v = value.strip()
        if _NUM_RE.match(v):
            v = v.replace(",", ".")
            return float(v) if "." in v else int(v)
    return value


def _parse_updates(updates_json: str) -> dict[str, Any]:
    data = json.loads(updates_json)
    if isinstance(data, list):
        data = {d["cell"]: d["value"] for d in data}
    if not isinstance(data, dict) or not data:
        raise ValueError('updates_json doit être un objet JSON, ex. {"B3": 42, "D10": "=SUM(D2:D9)"}')
    parsed: dict[str, Any] = {}
    for key, value in data.items():
        ref = str(key).strip().upper().replace("$", "")
        if not re.fullmatch(r"[A-Z]{1,3}[1-9]\d*", ref):
            raise ValueError(f"Référence de cellule invalide : {key}")
        parsed[ref] = _coerce(value)
    return parsed


def _edit_xlsx(src: Path, dst: Path, sheet: str | None, updates: dict[str, Any]) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(src, keep_vba=src.suffix.lower() == ".xlsm")
    if sheet:
        match = [n for n in wb.sheetnames if n.lower() == sheet.strip().lower()]
        if not match:
            raise KeyError(f"Feuille « {sheet} » introuvable. Feuilles : {wb.sheetnames}")
        ws = wb[match[0]]
    else:
        ws = wb.active
    for ref, value in updates.items():
        ws[ref].value = value
    wb.save(dst)
    return ws.title


def _readback(path: Path, sheet: str, refs: list[str]) -> list[str]:
    """Relit les cellules modifiées (formules recalculées via LibreOffice si disponible)."""
    import pandas as pd
    from openpyxl.utils import column_index_from_string
    from openpyxl.utils.cell import coordinate_from_string

    probe = path
    if path.suffix.lower() in (".xlsx", ".xlsm") and find_soffice():
        try:
            probe = _soffice_convert(path, "ods", TMP_DIR / f"rb_{uuid.uuid4().hex[:8]}")
        except Exception:  # noqa: BLE001
            probe = path
    sheets = pd.read_excel(probe, sheet_name=None, header=None, engine=_engine(probe))
    name = _pick_sheet(sheets, sheet)
    df = sheets[name]
    lines = []
    for ref in refs:
        col, row = coordinate_from_string(ref)
        r, c = row - 1, column_index_from_string(col) - 1
        value = df.iat[r, c] if r < df.shape[0] and c < df.shape[1] else None
        lines.append(f"{name}!{ref} = {value}")
    if probe != path:
        shutil.rmtree(probe.parent, ignore_errors=True)
    return lines


def _set_spreadsheet_cells(filename: str, updates_json: str, sheet: str | None, output_filename: str | None) -> str:
    path = _safe_path(filename)
    updates = _parse_updates(updates_json)
    ext = path.suffix.lower()
    out = _output_path(path, output_filename)
    note = ""
    if ext in (".xlsx", ".xlsm"):
        title = _edit_xlsx(path, out, sheet, updates)
    elif ext == ".ods" and find_soffice():
        work = TMP_DIR / f"ods_{uuid.uuid4().hex[:8]}"
        try:
            xlsx = _soffice_convert(path, "xlsx", work)
            title = _edit_xlsx(xlsx, xlsx, sheet, updates)
            converted = _soffice_convert(xlsx, "ods", work / "out")
            shutil.move(str(converted), str(out))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        note = " (édition via LibreOffice : mise en forme et formules conservées)"
    elif ext == ".ods":
        import pandas as pd
        from openpyxl.utils import column_index_from_string
        from openpyxl.utils.cell import coordinate_from_string

        sheets = pd.read_excel(path, sheet_name=None, header=None, engine="odf")
        title = _pick_sheet(sheets, sheet)
        df = sheets[title]
        for ref, value in updates.items():
            col, row = coordinate_from_string(ref)
            r, c = row - 1, column_index_from_string(col) - 1
            while df.shape[1] <= c:
                df[df.shape[1]] = None
            if df.shape[0] <= r:
                df = df.reindex(range(r + 1))
            df = df.astype(object)
            df.iat[r, c] = value
        sheets[title] = df
        _write_sheets(out, sheets, header_sheet=None)
        note = " (sans LibreOffice : valeurs seulement, mise en forme perdue, formules écrites en texte)"
    else:
        raise ValueError("set_spreadsheet_cells supporte .ods, .xlsx et .xlsm.")
    check = _readback(out, title, list(updates))
    return "\n".join([f"[OK] Nouveau fichier : {out}{note}", "Vérification des cellules :", *check])


# ---
# Implémentations : documents Writer / Word / PDF
# ---
def _odf_blocks(path: Path) -> list[str]:
    from odf import teletype
    from odf.namespaces import TABLENS, TEXTNS
    from odf.opendocument import load

    doc = load(str(path))
    blocks: list[str] = []

    def walk(node: Any) -> None:
        for child in node.childNodes:
            qname = getattr(child, "qname", None)
            if not qname:
                continue
            if qname[0] == TEXTNS and qname[1] in ("p", "h"):
                text = teletype.extractText(child)
                if qname[1] == "h":
                    level = int(child.getAttrNS(TEXTNS, "outline-level") or 1)
                    text = "#" * level + " " + text
                blocks.append(text)
            elif qname == (TABLENS, "table-row"):
                cells = [teletype.extractText(c) for c in child.childNodes
                         if getattr(c, "qname", (None, None))[1] == "table-cell"]
                blocks.append(" | ".join(cells))
            else:
                walk(child)

    walk(doc.body)
    return blocks


def _docx_blocks(path: Path) -> list[str]:
    import docx
    from docx.text.paragraph import Paragraph

    doc = docx.Document(str(path))
    items = list(doc.iter_inner_content()) if hasattr(doc, "iter_inner_content") else [*doc.paragraphs, *doc.tables]
    blocks: list[str] = []
    for item in items:
        if isinstance(item, Paragraph):
            style = (item.style.name if item.style is not None else "") or ""
            text = item.text
            if style.lower().startswith(("heading", "titre")):
                m = re.search(r"\d", style)
                text = "#" * (int(m.group()) if m else 1) + " " + text
            blocks.append(text)
        else:
            for row in item.rows:
                blocks.append(" | ".join(cell.text.strip() for cell in row.cells))
    return blocks


def _document_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".odt", ".ott", ".odp", ".ods", ".fodt"):
        return "\n".join(_odf_blocks(path))
    if ext in (".docx", ".docm", ".dotx"):
        return "\n".join(_docx_blocks(path))
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join(f"--- page {i} ---\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages, 1))
    if ext in (".txt", ".md", ".csv", ".html", ".xml", ".json"):
        return _read_text(path)[0]
    if ext in (".doc", ".rtf", ".ppt", ".pptx") and find_soffice():
        converted = _soffice_convert(path, "txt:Text (encoded):UTF8", TMP_DIR / f"txt_{uuid.uuid4().hex[:8]}")
        text = _read_text(converted)[0]
        shutil.rmtree(converted.parent, ignore_errors=True)
        return text
    raise ValueError(f"Format non supporté par read_document : {ext}")


def _read_document(filename: str, start_char: int, max_chars: int) -> str:
    path = _safe_path(filename)
    text = _document_text(path)
    start = max(0, start_char)
    chunk = text[start : start + max(200, max_chars)]
    return (
        f"Document : {path} | {_text_stats(text)} (calculé sur le texte extrait)\n"
        f"Extrait [{start}:{start + len(chunk)}] (# = titres, | = cellules de tableau) :\n-----\n{chunk}"
    )


_NS_PREFIX = {
    "http://purl.org/dc/elements/1.1/": "dc",
    "urn:oasis:names:tc:opendocument:xmlns:meta:1.0": "meta",
    "urn:oasis:names:tc:opendocument:xmlns:office:1.0": "office",
    "http://www.w3.org/1999/xlink": "xlink",
    "http://schemas.openxmlformats.org/package/2006/metadata/core-properties": "cp",
    "http://purl.org/dc/terms/": "dcterms",
    "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties": "app",
    "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties": "custom",
    "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes": "vt",
}
_ODF_LABELS = {
    "meta:initial-creator": "Auteur initial (créateur)",
    "dc:creator": "Dernier auteur ayant enregistré",
    "meta:creation-date": "Date de création",
    "dc:date": "Date de dernière modification",
    "meta:generator": "Logiciel / version (générateur)",
    "meta:editing-cycles": "Nombre d'enregistrements (révisions)",
    "meta:editing-duration": "Durée totale d'édition",
    "meta:print-date": "Dernière impression",
    "meta:printed-by": "Imprimé par",
    "dc:title": "Titre", "dc:subject": "Sujet", "dc:description": "Commentaires", "dc:language": "Langue",
    "meta:keyword": "Mot-clé",
    "meta:page-count": "Pages", "meta:word-count": "Mots", "meta:character-count": "Caractères (espaces compris)",
    "meta:non-whitespace-character-count": "Caractères (hors espaces)", "meta:paragraph-count": "Paragraphes",
    "meta:table-count": "Tableaux", "meta:image-count": "Images", "meta:object-count": "Objets",
    "meta:cell-count": "Cellules", "meta:sentence-count": "Phrases", "meta:syllable-count": "Syllabes",
}
_OOXML_LABELS = {
    "dc:creator": "Auteur (créateur)", "cp:lastModifiedBy": "Dernier auteur ayant enregistré",
    "dcterms:created": "Date de création", "dcterms:modified": "Date de dernière modification",
    "cp:revision": "Numéro de révision", "cp:lastPrinted": "Dernière impression",
    "dc:title": "Titre", "dc:subject": "Sujet", "dc:description": "Commentaires", "cp:keywords": "Mots-clés",
    "cp:category": "Catégorie", "dc:language": "Langue",
    "app:Application": "Logiciel générateur", "app:AppVersion": "Version du logiciel",
    "app:TotalTime": "Temps total d'édition (minutes)", "app:Pages": "Pages", "app:Words": "Mots",
    "app:Characters": "Caractères (hors espaces)", "app:CharactersWithSpaces": "Caractères (espaces compris)",
    "app:Lines": "Lignes", "app:Paragraphs": "Paragraphes", "app:Company": "Société", "app:Template": "Modèle",
}


def _qn(tag: str) -> str:
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return f"{_NS_PREFIX.get(ns, ns)}:{local}"
    return tag


def _iso_duration(value: str) -> str:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", value or "")
    if not m:
        return value
    d, h, mi, s = (float(x) if x else 0 for x in m.groups())
    total = int(d * 86400 + h * 3600 + mi * 60 + s)
    return f"{value} (= {total // 3600} h {total % 3600 // 60} min {total % 60} s)"


def _document_metadata(filename: str) -> str:
    path = _safe_path(filename)
    st = path.stat()
    lines = [f"Fichier : {path}", f"Taille : {_sizes(st.st_size)} | modifié (disque) : {_mtime(path)}"]
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        lines.append(f"Pages : {len(reader.pages)}")
        for key, value in (reader.metadata or {}).items():
            lines.append(f"{key} : {value}")
        return "\n".join(lines)
    if not zipfile.is_zipfile(path):
        raise ValueError("Ni document OpenDocument (.odt/.ods/.odp) ni Office Open XML (.docx/.xlsx/.pptx) ni PDF.")
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if "meta.xml" in names:  # ---------- OpenDocument (LibreOffice)
            mimetype = zf.read("mimetype").decode(errors="replace") if "mimetype" in names else "?"
            lines.append(f"Format : OpenDocument ({mimetype}) — source : meta.xml")
            root = ET.fromstring(zf.read("meta.xml"))
            office_meta = next((el for el in root if _qn(el.tag) == "office:meta"), root)
            for el in office_meta:
                key = _qn(el.tag)
                if key == "meta:document-statistic":
                    lines.append("--- Statistiques enregistrées (meta:document-statistic) ---")
                    for attr, value in el.attrib.items():
                        k = _qn(attr)
                        lines.append(f"{_ODF_LABELS.get(k, k)} [{k}] : {value}")
                elif key == "meta:user-defined":
                    name = next((v for a, v in el.attrib.items() if _qn(a) == "meta:name"), "?")
                    lines.append(f"Propriété personnalisée « {name} » : {el.text}")
                elif key == "meta:template":
                    attrs = {_qn(a): v for a, v in el.attrib.items()}
                    lines.append(f"Modèle : {attrs.get('xlink:title', '')} {attrs.get('xlink:href', '')}")
                else:
                    value = _iso_duration(el.text or "") if key == "meta:editing-duration" else (el.text or "")
                    lines.append(f"{_ODF_LABELS.get(key, key)} [{key}] : {value}")
        elif "docProps/core.xml" in names:  # ---------- Office Open XML (Word/Excel)
            lines.append("Format : Office Open XML — sources : docProps/core.xml, app.xml, custom.xml")
            for part in ("docProps/core.xml", "docProps/app.xml"):
                if part not in names:
                    continue
                lines.append(f"--- {part} ---")
                for el in ET.fromstring(zf.read(part)):
                    if len(el):  # vecteurs (HeadingPairs, TitlesOfParts…) ignorés
                        continue
                    key = _qn(el.tag)
                    lines.append(f"{_OOXML_LABELS.get(key, key)} [{key}] : {el.text or ''}")
            if "docProps/custom.xml" in names:
                lines.append("--- Propriétés personnalisées ---")
                for prop in ET.fromstring(zf.read("docProps/custom.xml")):
                    value = next((child.text for child in prop), "")
                    lines.append(f"{prop.attrib.get('name', '?')} : {value}")
        else:
            raise ValueError("Archive ZIP sans meta.xml ni docProps/core.xml : pas un document bureautique.")
        media = [n for n in names if n.startswith(("Pictures/", "word/media/", "xl/media/", "ppt/media/"))]
        if media:
            lines.append(f"Images intégrées ({len(media)}) : " + ", ".join(media[:20]))
    if ext in (".odt", ".docx"):
        lines.append("--- Statistiques recalculées sur le texte réel (peuvent différer des valeurs enregistrées) ---")
        lines.append(_text_stats(_document_text(path)))
    return "\n".join(lines)


def _docx_paragraphs(doc: Any) -> list[Any]:
    seen: set[int] = set()
    result: list[Any] = []

    def visit(container: Any) -> None:
        for p in container.paragraphs:
            if id(p._p) not in seen:
                seen.add(id(p._p))
                result.append(p)
        for table in getattr(container, "tables", []):
            for row in table.rows:
                for cell in row.cells:
                    visit(cell)

    visit(doc)
    for section in doc.sections:
        for attr in ("header", "footer", "first_page_header", "first_page_footer", "even_page_header", "even_page_footer"):
            try:
                visit(getattr(section, attr))
            except Exception:  # noqa: BLE001
                pass
    return result


def _replace_text_in_document(filename: str, search: str, replacement: str, match_case: bool,
                              output_filename: str | None) -> str:
    if not search:
        raise ValueError("Le texte à rechercher est vide.")
    path = _safe_path(filename)
    out = _output_path(path, output_filename)
    pattern = re.compile(re.escape(search), 0 if match_case else re.IGNORECASE)
    repl = lambda _m: replacement  # noqa: E731 — évite l'interprétation des backslashes
    ext = path.suffix.lower()
    note = ""
    if ext in (".docx", ".docm"):
        import docx

        doc = docx.Document(str(path))
        count = 0
        for p in _docx_paragraphs(doc):
            runs = p.runs
            n = len(pattern.findall("".join(r.text for r in runs)))
            if not n:
                continue
            done = 0
            for run in runs:
                run.text, k = pattern.subn(repl, run.text)
                done += k
            if done < n:  # occurrence à cheval sur plusieurs « runs » (changement de mise en forme)
                merged = pattern.sub(repl, "".join(r.text for r in runs))
                runs[0].text = merged
                for r in runs[1:]:
                    r.text = ""
            count += n
        doc.save(str(out))
    elif ext in (".odt", ".ods", ".odp"):
        from odf import teletype
        from odf.opendocument import load

        doc = load(str(path))

        def walk(node: Any) -> int:
            total = 0
            for child in list(node.childNodes):
                if child.nodeType == child.TEXT_NODE:
                    new, k = pattern.subn(repl, child.data)
                    if k:
                        child.data = new
                        total += k
                else:
                    total += walk(child)
            return total

        count = walk(doc.body) + (walk(doc.masterstyles) if doc.masterstyles is not None else 0)
        remaining = len(pattern.findall(teletype.extractText(doc.body)))
        if remaining:
            note = f"\n[WARN] {remaining} occurrence(s) coupée(s) par un changement de style non remplacée(s) : utilisez run_python_on_downloads."
        doc.save(str(out))
    elif ext in (".txt", ".csv", ".md", ".html", ".xml", ".json"):
        text, enc = _read_text(path)
        new, count = pattern.subn(repl, text)
        out.write_text(new, encoding="utf-8" if enc.startswith("utf-8") else enc)
    else:
        raise ValueError(f"Remplacement non supporté pour {ext} (supportés : .odt .ods .odp .docx .txt .csv .md).")
    return f"[OK] {count} remplacement(s) « {search} » -> « {replacement} ». Nouveau fichier : {out}{note}"


def _apply_text_style(filename: str, contains_text: str | None, all_paragraphs: bool, bold: bool | None,
                      italic: bool | None, underline: bool | None, font_size_pt: float | None,
                      font_name: str | None, color_hex: str | None, paragraph_style: str | None,
                      output_filename: str | None) -> str:
    if not contains_text and not all_paragraphs:
        raise ValueError("Précisez contains_text (paragraphes ciblés) ou all_paragraphs=true.")
    path = _safe_path(filename)
    out = _output_path(path, output_filename)
    needle = (contains_text or "").lower()
    color = color_hex.lstrip("#").upper() if color_hex else None
    if color and not re.fullmatch(r"[0-9A-F]{6}", color):
        raise ValueError("color_hex doit être de la forme #RRGGBB.")
    ext = path.suffix.lower()
    modified = 0
    if ext in (".docx", ".docm"):
        import docx
        from docx.shared import Pt, RGBColor

        doc = docx.Document(str(path))
        for p in _docx_paragraphs(doc):
            if not all_paragraphs and needle not in p.text.lower():
                continue
            if paragraph_style:
                try:
                    p.style = doc.styles[paragraph_style]
                except KeyError as exc:
                    names = [s.name for s in doc.styles if s.type == 1][:40]
                    raise KeyError(f"Style « {paragraph_style} » absent. Styles : {names}") from exc
            for run in p.runs:
                if bold is not None:
                    run.font.bold = bold
                if italic is not None:
                    run.font.italic = italic
                if underline is not None:
                    run.font.underline = underline
                if font_size_pt:
                    run.font.size = Pt(font_size_pt)
                if font_name:
                    run.font.name = font_name
                if color:
                    run.font.color.rgb = RGBColor.from_string(color)
            modified += 1
        doc.save(str(out))
    elif ext == ".odt":
        from odf import teletype
        from odf.namespaces import STYLENS, TEXTNS
        from odf.opendocument import load
        from odf.style import FontFace, Style, TextProperties

        doc = load(str(path))
        props: dict[str, str] = {}
        if bold is not None:
            props.update(fontweight="bold" if bold else "normal", fontweightasian="bold" if bold else "normal",
                         fontweightcomplex="bold" if bold else "normal")
        if italic is not None:
            props.update(fontstyle="italic" if italic else "normal")
        if underline is not None:
            props.update(textunderlinestyle="solid" if underline else "none")
            if underline:
                props.update(textunderlinewidth="auto", textunderlinecolor="font-color")
        if font_size_pt:
            props["fontsize"] = f"{font_size_pt:g}pt"
        if color:
            props["color"] = f"#{color}"
        if font_name:
            declared = {f.getAttrNS(STYLENS, "name") for f in doc.fontfacedecls.getElementsByType(FontFace)}
            if font_name not in declared:
                doc.fontfacedecls.addElement(FontFace(name=font_name, fontfamily=f"'{font_name}'"))
            props["fontname"] = font_name

        target_style = None
        if paragraph_style:
            wanted = paragraph_style.strip().lower()
            for st in doc.styles.getElementsByType(Style):
                name = st.getAttrNS(STYLENS, "name") or ""
                disp = st.getAttrNS(STYLENS, "display-name") or ""
                if wanted in {name.lower(), disp.lower(), name.replace("_20_", " ").lower()}:
                    target_style = name
                    break
            if not target_style:
                raise KeyError(f"Style « {paragraph_style} » introuvable (utilisez les noms anglais : "
                               "Heading 1, Text Body, Title…).")

        existing = {s.getAttrNS(STYLENS, "name") for s in doc.automaticstyles.childNodes if hasattr(s, "getAttrNS")}
        cache: dict[str | None, str] = {}

        def auto_style(parent: str | None) -> str:
            if parent not in cache:
                i = 1
                while f"AgentP{i}" in existing:
                    i += 1
                name = f"AgentP{i}"
                existing.add(name)
                st = Style(name=name, family="paragraph", **({"parentstylename": parent} if parent else {}))
                st.addElement(TextProperties(**props))
                doc.automaticstyles.addElement(st)
                cache[parent] = name
            return cache[parent]

        def walk(node: Any) -> None:
            nonlocal modified
            for child in node.childNodes:
                qname = getattr(child, "qname", None)
                if not qname:
                    continue
                if qname[0] == TEXTNS and qname[1] in ("p", "h"):
                    if all_paragraphs or needle in teletype.extractText(child).lower():
                        parent = target_style or child.getAttrNS(TEXTNS, "style-name")
                        child.setAttrNS(TEXTNS, "style-name", auto_style(parent) if props else parent)
                        modified += 1
                else:
                    walk(child)

        walk(doc.body)
        doc.save(str(out))
    else:
        raise ValueError("apply_text_style supporte .odt et .docx.")
    if not modified:
        return f"[WARN] Aucun paragraphe ne contient « {contains_text} ». Fichier écrit sans changement : {out}"
    return f"[OK] {modified} paragraphe(s) mis en forme. Nouveau fichier : {out}"


def _convert_document(filename: str, target_format: str) -> str:
    allowed = {"pdf", "odt", "docx", "doc", "rtf", "txt", "html", "ods", "xlsx", "csv", "odp", "pptx", "png"}
    target = target_format.strip().lower().lstrip(".")
    if target not in allowed:
        raise ValueError(f"Format cible non supporté : {target}. Autorisés : {sorted(allowed)}")
    path = _safe_path(filename)
    if path.suffix.lower().lstrip(".") == target:
        raise ValueError("Le fichier est déjà dans ce format.")
    out = _soffice_convert(path, target, path.parent)
    return f"[OK] Converti : {out} | {_sizes(out.stat().st_size)}"


# ---
# Exécution Python confinée
# ---
# Script d'amorçage exécuté dans un processus Python séparé (python -I) :
#  1) analyse statique : imports réseau/processus et eval/exec/__import__ refusés ;
#  2) hook d'audit (PEP 578) : toute écriture/suppression/renommage hors ./downloads, tout sous-processus
#     et toute connexion réseau lèvent PermissionError ; lectures autorisées uniquement dans ./downloads
#     et dans les répertoires de l'interpréteur (bibliothèques), polices et fuseaux horaires.
# C'est un confinement « best effort » adapté à un agent, pas une frontière de sécurité contre un attaquant.
_SANDBOX_BOOTSTRAP = r'''
import ast, os, sys
sys.dont_write_bytecode = True
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
try:
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT_REAL = os.path.realpath(sys.argv[1])
ROOT = os.path.normcase(ROOT_REAL)
CODE = sys.stdin.read()
BANNED_MODULES = {"subprocess", "socket", "ssl", "ctypes", "multiprocessing", "requests", "urllib", "urllib3",
                  "http", "httpx", "aiohttp", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "paramiko",
                  "webbrowser", "winreg", "_winapi", "pty", "asyncio", "selenium", "playwright"}
BANNED_CALLS = {"__import__", "eval", "exec", "compile", "breakpoint", "input", "help"}
try:
    TREE = ast.parse(CODE, "<agent_code>", "exec")
except SyntaxError as exc:
    print(f"SyntaxError: {exc}", file=sys.stderr)
    sys.exit(2)
for node in ast.walk(TREE):
    mods = []
    if isinstance(node, ast.Import):
        mods = [a.name for a in node.names]
    elif isinstance(node, ast.ImportFrom) and node.module:
        mods = [node.module]
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
        print(f"SandboxError: appel interdit : {node.func.id}()", file=sys.stderr)
        sys.exit(3)
    for m in mods:
        if m.split(".")[0] in BANNED_MODULES:
            print(f"SandboxError: import interdit : {m}", file=sys.stderr)
            sys.exit(3)

def _norm(p):
    if isinstance(p, bytes):
        p = os.fsdecode(p)
    return os.path.normcase(os.path.realpath(os.path.join(os.getcwd(), os.fspath(p))))

READ_ROOTS = [ROOT]
_extra = [sys.prefix, sys.base_prefix, sys.exec_prefix, *sys.path,
          os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts"),
          "/usr/share/fonts", "/usr/local/share/fonts", "/etc/fonts", "/usr/share/zoneinfo", "/etc/localtime"]
for _p in _extra:
    if _p and os.path.exists(_p):
        READ_ROOTS.append(os.path.normcase(os.path.realpath(_p)))
WRITE_ROOTS = [ROOT]
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_TRUNC", 0)
BLOCKED_PREFIXES = ("subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.startfile",
                    "os.fork", "os.forkpty", "os.kill", "os.killpg", "socket.", "webbrowser.", "winreg.", "_winapi.")
PATH_EVENTS = {"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate",
               "os.utime", "os.link", "os.symlink", "os.chdir", "os.chflags", "shutil.rmtree", "shutil.move",
               "shutil.copyfile", "shutil.copytree", "shutil.chown", "shutil.make_archive", "shutil.unpack_archive"}
LIST_EVENTS = {"os.listdir", "os.scandir", "glob.glob", "glob.glob/2"}

def _inside(p, roots):
    if p is None or isinstance(p, int):
        return True
    try:
        n = _norm(p)
    except Exception:
        return False
    return any(n == r or n.startswith(r.rstrip("\\/") + os.sep) for r in roots)

_state = {"busy": False}

def _hook(event, args):
    if _state["busy"]:
        return
    _state["busy"] = True
    try:
        if event.startswith(BLOCKED_PREFIXES):
            raise PermissionError(f"SandboxError: opération interdite ({event})")
        if event == "open":
            path = args[0] if args else None
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and bool(flags & WRITE_FLAGS))
            if not _inside(path, WRITE_ROOTS if writing else READ_ROOTS):
                kind = "écriture" if writing else "lecture"
                raise PermissionError(f"SandboxError: {kind} refusée hors de ./downloads : {path}")
        elif event in PATH_EVENTS:
            for a in args:
                if isinstance(a, (str, bytes, os.PathLike)) and not _inside(a, WRITE_ROOTS):
                    raise PermissionError(f"SandboxError: {event} refusé hors de ./downloads : {a}")
        elif event in LIST_EVENTS:
            if args and isinstance(args[0], (str, bytes, os.PathLike)) and not _inside(args[0], READ_ROOTS):
                raise PermissionError(f"SandboxError: listage refusé hors de ./downloads : {args[0]}")
    finally:
        _state["busy"] = False

os.chdir(ROOT_REAL)
sys.addaudithook(_hook)
_globals = {"__name__": "__main__", "__builtins__": __builtins__, "DOWNLOADS": ROOT_REAL}
exec(compile(TREE, "<agent_code>", "exec"), _globals)
'''

_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH", re.IGNORECASE)


def _run_python(code: str, timeout_s: int) -> ActionResult:
    timeout_s = max(5, min(int(timeout_s), 180))
    before = {p: p.stat().st_mtime for p in _iter_files()}
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
    env.update(MPLCONFIGDIR=str(TMP_DIR / "mpl"), MPLBACKEND="Agg")
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _SANDBOX_BOOTSTRAP, str(DOWNLOADS_DIR)],
            input=code, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_s, cwd=str(DOWNLOADS_DIR), env=env,
        )
    except subprocess.TimeoutExpired:
        return _err(f"Timeout : le script a dépassé {timeout_s}s et a été interrompu.")
    elapsed = time.perf_counter() - started
    changed = [p for p in _iter_files() if before.get(p) != p.stat().st_mtime]
    parts = [f"Code de sortie : {proc.returncode} ({elapsed:.1f}s)", "--- stdout ---",
             proc.stdout.strip() or "(vide — utilisez print() pour afficher les résultats)"]
    if proc.stderr.strip():
        parts += ["--- stderr ---", proc.stderr.strip()[-3000:]]
    if changed:
        parts.append("Fichiers créés/modifiés : " + ", ".join(str(p) for p in changed))
    text = "\n".join(parts)
    return _ok(text) if proc.returncode == 0 else _err(text)


# ===
# ACTIONS EXPOSÉES À L'AGENT (la docstring = description vue par le LLM : courte et précise)
# ===
@agent_action
async def list_downloads(limit: int = 20, pattern: str = "*") -> ActionResult:
    """Lister les fichiers du dossier local downloads (plus récents d'abord) avec chemin absolu, taille et date.
    pattern = filtre type '*.ods'. À utiliser pour retrouver le nom exact d'un fichier téléchargé."""
    return await _run(_list_downloads, limit, pattern)


@agent_action
async def wait_for_new_download(timeout_s: int = 30, since_seconds: int = 120) -> ActionResult:
    """Attendre la fin d'un téléchargement (à appeler JUSTE APRÈS avoir cliqué sur un lien/bouton de téléchargement).
    Renvoie le(s) fichier(s) terminé(s) dans les since_seconds dernières secondes."""
    return await _run(_wait_for_download, timeout_s, since_seconds)


@agent_action
async def read_text_file(filename: str, start_char: int = 0, max_chars: int = 5000) -> ActionResult:
    """Lire un fichier texte (.txt .md .csv .json .xml .html…) de downloads, avec stats mots/caractères/lignes.
    Utiliser start_char pour paginer les gros fichiers."""
    return await _run(_read_text_file, filename, start_char, max_chars)


@agent_action
async def write_text_file(filename: str, content: str, append: bool = False) -> ActionResult:
    """Créer/écraser (ou compléter si append=true) un fichier texte UTF-8 dans downloads ; renvoie taille et SHA256."""
    return await _run(_write_text_file, filename, content, append)


@agent_action
async def read_csv_file(filename: str, max_rows: int = 30, separator: str = "auto") -> ActionResult:
    """Analyser un CSV (séparateur auto-détecté) : dimensions, types de colonnes, aperçu et statistiques numériques."""
    return await _run(_read_csv_file, filename, max_rows, separator)


@agent_action
async def file_info_and_hash(filename: str) -> ActionResult:
    """Taille EXACTE d'un fichier (octets, Ko, Kio, Mo, Mio, Go), type MIME, dates et empreintes MD5/SHA1/SHA256.
    Toujours utiliser cet outil pour toute question de taille ou de hash (ne jamais estimer)."""
    return await _run(_file_info_and_hash, filename)


@agent_action
async def inspect_zip(filename: str) -> ActionResult:
    """Lister le contenu d'une archive .zip sans l'extraire : noms, tailles, dates, CRC, chiffrement, totaux."""
    return await _run(_inspect_zip, filename)


@agent_action
async def extract_zip(filename: str, destination_folder: str | None = None, password: str | None = None) -> ActionResult:
    """Extraire une archive .zip dans downloads (par défaut dans un dossier du même nom). Protégé contre zip-slip."""
    return await _run(_extract_zip, filename, destination_folder, password)


@agent_action
async def create_zip(output_filename: str, files: str) -> ActionResult:
    """Créer une archive .zip dans downloads. files = liste séparée par des virgules (noms, dossiers ou motifs '*.png')."""
    return await _run(_create_zip, output_filename, files)


@agent_action
async def inspect_image(filename: str) -> ActionResult:
    """Inspecter une image locale (.png .jpg .gif .webp…) : format, dimensions en pixels, mode, DPI, EXIF/GPS, taille."""
    return await _run(_inspect_image, filename)


@agent_action
async def read_spreadsheet(filename: str, sheet: str | None = None, max_rows: int = 40,
                           show_formulas: bool = False) -> ActionResult:
    """Lire un tableur LibreOffice Calc / Excel (.ods .xlsx .xls .csv) sous forme de grille avec coordonnées A1 réelles.
    Sans sheet : 3 premières feuilles. show_formulas=true liste aussi les formules des cellules."""
    return await _run(_read_spreadsheet, filename, sheet, max_rows, show_formulas)


@agent_action
async def spreadsheet_stats(filename: str, sheet: str | None = None, columns: str | None = None,
                            header_row: int = 1) -> ActionResult:
    """Statistiques exactes par colonne d'un tableur (n, somme, moyenne, médiane, min, max, écart-type ; top valeurs
    pour le texte). columns = noms d'en-têtes séparés par des virgules ; header_row = n° de ligne des en-têtes."""
    return await _run(_spreadsheet_stats, filename, sheet, columns, header_row)


@agent_action
async def sort_filter_spreadsheet(filename: str, sheet: str | None = None, sort_by: str | None = None,
                                  ascending: bool = True, filter_query: str | None = None, header_row: int = 1,
                                  output_filename: str | None = None) -> ActionResult:
    """Trier et/ou filtrer une feuille et enregistrer un NOUVEAU fichier (même format). sort_by = colonnes séparées
    par des virgules ; filter_query = expression pandas, ex. "Prix > 10 and Ville == 'Lyon'" (`Nom colonne` si espaces)."""
    return await _run(_sort_filter_spreadsheet, filename, sheet, sort_by, ascending, filter_query, header_row,
                      output_filename)


@agent_action
async def set_spreadsheet_cells(filename: str, updates_json: str, sheet: str | None = None,
                                output_filename: str | None = None) -> ActionResult:
    """Modifier des cellules ou formules d'un .ods/.xlsx et enregistrer un NOUVEAU fichier prêt à réuploader.
    updates_json = objet JSON {"B3": 42, "C1": "Total", "D10": "=SUM(D2:D9)"} (formules en syntaxe anglaise).
    Renvoie les valeurs relues après recalcul pour vérification."""
    return await _run(_set_spreadsheet_cells, filename, updates_json, sheet, output_filename)


@agent_action
async def read_document(filename: str, start_char: int = 0, max_chars: int = 5000) -> ActionResult:
    """Extraire le texte d'un document Writer/Word/PDF (.odt .docx .pdf .doc .rtf) dans l'ordre (titres #, tableaux |)
    avec nombre de mots/caractères. Utiliser start_char pour paginer."""
    return await _run(_read_document, filename, start_char, max_chars)


@agent_action
async def document_metadata(filename: str) -> ActionResult:
    """Métadonnées internes d'un document (.odt .ods .odp via meta.xml ; .docx .xlsx via docProps ; .pdf) :
    auteur initial, dernier auteur, dates de création/modification, logiciel générateur, révisions, durée d'édition,
    nombres de pages/mots/caractères, propriétés personnalisées, images intégrées."""
    return await _run(_document_metadata, filename)


@agent_action
async def replace_text_in_document(filename: str, search: str, replacement: str, match_case: bool = True,
                                   output_filename: str | None = None) -> ActionResult:
    """Remplacer toutes les occurrences d'un texte dans un .odt/.docx/.ods/.txt/.csv (corps, tableaux, en-têtes,
    pieds de page) et enregistrer un NOUVEAU fichier. Renvoie le nombre de remplacements."""
    return await _run(_replace_text_in_document, filename, search, replacement, match_case, output_filename)


@agent_action
async def apply_text_style(filename: str, contains_text: str | None = None, all_paragraphs: bool = False,
                           bold: bool | None = None, italic: bool | None = None, underline: bool | None = None,
                           font_size_pt: float | None = None, font_name: str | None = None,
                           color_hex: str | None = None, paragraph_style: str | None = None,
                           output_filename: str | None = None) -> ActionResult:
    """Mettre en forme les paragraphes d'un .odt/.docx contenant contains_text (ou tous) : gras, italique, souligné,
    taille (pt), police, couleur #RRGGBB, style de paragraphe (noms anglais : 'Heading 1', 'Title'…). NOUVEAU fichier."""
    return await _run(_apply_text_style, filename, contains_text, all_paragraphs, bold, italic, underline,
                      font_size_pt, font_name, color_hex, paragraph_style, output_filename)


@agent_action
async def convert_document(filename: str, target_format: str = "pdf") -> ActionResult:
    """Convertir un fichier bureautique avec LibreOffice headless (soffice --convert-to) : .odt/.docx/.ods/.xlsx/.odp
    -> pdf, docx, odt, xlsx, ods, csv, txt, html… Le fichier converti est créé à côté de l'original."""
    return await _run(_convert_document, filename, target_format)


@agent_action
async def run_python_on_downloads(code: str, timeout_s: int = 60) -> ActionResult:
    """Exécuter un court script Python (pandas, openpyxl, odfpy, python-docx, PIL, zipfile, hashlib…) dans downloads
    (répertoire courant ; variable DOWNLOADS = chemin absolu). Afficher les résultats avec print().
    Réseau, sous-processus et accès hors downloads interdits. Pour calculs/transformations non couverts par les autres outils."""
    try:
        result = await asyncio.to_thread(_run_python, code, timeout_s)
    except Exception as exc:  # noqa: BLE001
        result = _err(f"{type(exc).__name__} : {exc}")
    refresh_available_file_paths()
    return result


async def ask_human(question: str) -> ActionResult:
    """Poser une question à l'humain dans le terminal et attendre sa réponse (connexion requise, captcha, 2FA,
    ambiguïté bloquante, confirmation avant validation finale). Ne pas abuser."""
    print("\n" + "=" * 80 + f"\nL'AGENT DEMANDE : {question}\n" + "=" * 80, flush=True)
    answer = await asyncio.to_thread(input, "Votre réponse (Entrée = continuer) > ")
    return _ok(f"Réponse de l'humain : {answer.strip() or '(continuer)'}")


interactive_tools = Tools()

if ALLOW_ASK_HUMAN:
    agent_action(ask_human)
    interactive_tools.action(inspect.cleandoc(ask_human.__doc__ or "ask_human"))(ask_human)

refresh_available_file_paths()

__all__ = [
    "tools", "controller", "interactive_tools", "DOWNLOADS_DIR", "AVAILABLE_FILE_PATHS", "ALLOW_ASK_HUMAN",
    "refresh_available_file_paths", "find_soffice",
]
