"""
local_kit.py — Boîte à outils synchrone importable directement dans le REPL browser-use 1.0 :

    import local_kit as lk

Expose en 1 ligne toutes les capacités bureautiques, archives, images (EXIF + ELA + découpe en grille),
conversion PDF LibreOffice headless et glisser-déposer, tout en restant confiné à ./downloads.
"""

from __future__ import annotations

import io
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

_DOWNLOADS = Path(__file__).resolve().parent
_BASE_DIR = _DOWNLOADS.parent
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

import tools as _t  # noqa: E402


# ---
# Téléchargements & fichiers standards
# ---
def ls(limit: int = 20, pattern: str = "*") -> str:
    """Liste les fichiers téléchargés ou produits dans downloads/ (du plus récent au plus ancien)."""
    return _t._list_downloads(limit, pattern)


def latest(wait_s: int = 12, since_s: int = 180) -> Path:
    """Attend la fin d'un téléchargement dans downloads/ et retourne le Path absolu du fichier le plus récent."""
    deadline = time.time() + max(1, min(int(wait_s), 180))
    threshold = time.time() - max(0, int(since_s))
    sizes: dict[Path, int] = {}
    while True:
        files = _t._iter_files()
        partial = [p for p in files if p.suffix.lower() in _t.PARTIAL_SUFFIXES]
        ready = [p for p in files if p.suffix.lower() not in _t.PARTIAL_SUFFIXES]
        recent = [p for p in ready if p.stat().st_mtime >= threshold]
        if recent and not partial and all(sizes.get(p) == p.stat().st_size for p in recent):
            return recent[0]
        sizes = {p: p.stat().st_size for p in recent}
        if time.time() >= deadline:
            if recent:
                return recent[0]
            if ready:
                return ready[0]
            raise FileNotFoundError(
                f"Aucun fichier trouvé dans {_DOWNLOADS} (téléchargements en cours : {[p.name for p in partial]})."
            )
        time.sleep(0.5)


def info(filename: str | Path) -> str:
    """Taille EXACTE (octets, Ko, Kio, Mo, Mio, Go), type MIME, dates et empreintes MD5/SHA1/SHA256."""
    return _t._file_info_and_hash(str(filename))


def read_text(filename: str | Path, start: int = 0, max_chars: int = 5000) -> str:
    """Lit un fichier texte (.txt .md .csv .json .xml .html…) avec comptage exact mots/caractères/lignes."""
    return _t._read_text_file(str(filename), start, max_chars)


def write_text(filename: str | Path, content: str, append: bool = False) -> str:
    """Crée ou met à jour un fichier texte UTF-8 dans downloads/."""
    return _t._write_text_file(str(filename), content, append)


def read_csv(filename: str | Path, max_rows: int = 30, sep: str = "auto") -> str:
    """Lit et analyse un CSV (séparateur auto-détecté) : dimensions, colonnes, aperçu, stats numériques."""
    return _t._read_csv_file(str(filename), max_rows, sep)


def zip_list(filename: str | Path) -> str:
    """Liste le contenu d'une archive .zip sans l'extraire (tailles, dates, CRC, totaux)."""
    return _t._inspect_zip(str(filename))


def unzip(filename: str | Path, dest: str | None = None, password: str | None = None) -> str:
    """Extrait une archive .zip dans downloads/ (protégé contre zip-slip)."""
    return _t._extract_zip(str(filename), dest, password)


def make_zip(output_filename: str | Path, files: str | list[str | Path]) -> str:
    """Crée une archive .zip dans downloads/. files = chaîne séparée par des virgules ou liste de chemins."""
    joined = ", ".join(str(x) for x in files) if isinstance(files, (list, tuple)) else str(files)
    return _t._create_zip(str(output_filename), joined)


# ---
# Images : métadonnées EXIF, Error Level Analysis (détection de retouches), découpe en grille
# ---
def img_info(filename: str | Path) -> str:
    """Format, dimensions (px), mode, DPI, EXIF/GPS, logiciel, commentaires et taille exacte d'une image."""
    return _t._inspect_image(str(filename))


def ela(filename: str | Path, quality: int = 90, scale: int = 18) -> str:
    """Génère une carte Error Level Analysis (ELA) pour faire ressortir les zones retouchées d'une photo.
    Renvoie le chemin absolu du PNG produit (à afficher ensuite avec show_image(path))."""
    from PIL import Image, ImageChops, ImageEnhance

    src = _t._safe_path(str(filename))
    out = _t.TMP_DIR / f"{src.stem}_ela.png"
    with Image.open(src) as orig:
        rgb = orig.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality)
        buf.seek(0)
        with Image.open(buf) as recomp:
            diff = ImageChops.difference(rgb, recomp.convert("RGB"))
        extrema = diff.getextrema()
        max_diff = max(hi for _, hi in extrema) or 1
        factor = max(1.0, min(float(scale), 255.0 / max_diff))
        enhanced = ImageEnhance.Brightness(diff).enhance(factor)
        enhanced.save(out, format="PNG")
    return str(out)


def crop_grid(filename: str | Path, rows: int = 3, cols: int = 3) -> list[str]:
    """Découpe une image en grille rows x cols (de gauche à droite, de haut en bas) dans .agent_tmp/
    et renvoie la liste des chemins PNG (utile pour zoomer des zones avec show_image(...))."""
    from PIL import Image

    src = _t._safe_path(str(filename))
    paths: list[str] = []
    with Image.open(src) as img:
        w, h = img.size
        cw, ch = w / cols, h / rows
        idx = 1
        for r in range(rows):
            for c in range(cols):
                box = (int(c * cw), int(r * ch), int((c + 1) * cw), int((r + 1) * ch))
                out = _t.TMP_DIR / f"{src.stem}_zone_{idx}.png"
                img.crop(box).save(out, format="PNG")
                paths.append(str(out))
                idx += 1
    return paths


# ---
# Tableurs LibreOffice Calc / Excel (.ods, .xlsx, .xls, .csv)
# ---
def sheet(filename: str | Path, sheet: str | None = None, max_rows: int = 40, formulas: bool = False) -> str:
    """Lit un tableur avec coordonnées A1 réelles (et les formules si formulas=True)."""
    return _t._read_spreadsheet(str(filename), sheet, max_rows, formulas)


def stats(filename: str | Path, sheet: str | None = None, cols: str | list[str] | None = None,
          header_row: int = 1) -> str:
    """Calcule les statistiques exactes (somme, moyenne, médiane, min, max, écart-type, top valeurs) par colonne."""
    col_str = ", ".join(cols) if isinstance(cols, (list, tuple)) else cols
    return _t._spreadsheet_stats(str(filename), sheet, col_str, header_row)


def sort_filter(filename: str | Path, sheet: str | None = None, sort_by: str | list[str] | None = None,
                ascending: bool = True, query: str | None = None, header_row: int = 1,
                out: str | None = None) -> str:
    """Trie et/ou filtre une feuille et enregistre un NOUVEAU fichier (*_modifie.*)."""
    sb = ", ".join(sort_by) if isinstance(sort_by, (list, tuple)) else sort_by
    return _t._sort_filter_spreadsheet(str(filename), sheet, sb, ascending, query, header_row, out)


def edit_cells(filename: str | Path, updates: dict[str, Any] | str, sheet: str | None = None,
               out: str | None = None) -> str:
    """Modifie des cellules/formules dans un .ods/.xlsx et enregistre un NOUVEAU fichier prêt à réuploader.
    updates = dict Python {'B3': 42, 'D10': '=SUM(D2:D9)'} (ou JSON). Renvoie les valeurs recalculées."""
    payload = updates if isinstance(updates, str) else json.dumps(updates, ensure_ascii=False)
    return _t._set_spreadsheet_cells(str(filename), payload, sheet, out)


# ---
# Documents LibreOffice Writer / Word / PDF (.odt, .docx, .pdf, .doc, .rtf)
# ---
def doc(filename: str | Path, start: int = 0, max_chars: int = 5000) -> str:
    """Extrait le texte d'un document .odt/.docx/.pdf/.doc/.rtf avec statistiques mots/caractères."""
    return _t._read_document(str(filename), start, max_chars)


def meta(filename: str | Path) -> str:
    """Lit les métadonnées internes (meta.xml, docProps, PDF) : auteur, dates, logiciel, révisions, stats."""
    return _t._document_metadata(str(filename))


def replace_doc(filename: str | Path, search: str, replacement: str, match_case: bool = True,
                out: str | None = None) -> str:
    """Remplace du texte dans un .odt/.docx/.ods/.txt/.csv et enregistre un NOUVEAU fichier (*_modifie.*)."""
    return _t._replace_text_in_document(str(filename), search, replacement, match_case, out)


def style_doc(filename: str | Path, contains_text: str | None = None, all_paragraphs: bool = False,
              bold: bool | None = None, italic: bool | None = None, underline: bool | None = None,
              font_size_pt: float | None = None, font_name: str | None = None,
              color_hex: str | None = None, paragraph_style: str | None = None,
              out: str | None = None) -> str:
    """Applique une mise en forme (gras, italique, taille, police, couleur, style) dans un .odt/.docx."""
    return _t._apply_text_style(
        str(filename), contains_text, all_paragraphs, bold, italic, underline,
        font_size_pt, font_name, color_hex, paragraph_style, out,
    )


def convert(filename: str | Path, target_format: str = "pdf") -> str:
    """Convertit un fichier bureautique via LibreOffice headless (pdf, docx, odt, xlsx, ods, csv, txt…)."""
    return _t._convert_document(str(filename), target_format)


def to_pdf(filename: str | Path) -> str:
    """Convertit un document/tableur en PDF via LibreOffice headless."""
    return convert(filename, "pdf")


# ---
# Glisser-déposer fluide via CDP (exercices d'appariement / classement)
# ---
def drag(cdp_fn: Callable[..., Any], x1: float, y1: float, x2: float, y2: float, steps: int = 12) -> str:
    """Glisser-déposer fluide de (x1, y1) vers (x2, y2) via la fonction `cdp` du REPL."""
    cdp_fn("Input.dispatchMouseEvent", type="mouseMoved", x=float(x1), y=float(y1))
    cdp_fn("Input.dispatchMouseEvent", type="mousePressed", x=float(x1), y=float(y1), button="left", clickCount=1)
    n = max(2, int(steps))
    for i in range(1, n + 1):
        t = i / n
        cx = x1 + (x2 - x1) * t
        cy = y1 + (y2 - y1) * t
        cdp_fn("Input.dispatchMouseEvent", type="mouseMoved", x=float(cx), y=float(cy), buttons=1)
        time.sleep(0.03)
    cdp_fn("Input.dispatchMouseEvent", type="mouseReleased", x=float(x2), y=float(y2), button="left", clickCount=1)
    return f"dragged ({x1:.0f},{y1:.0f}) -> ({x2:.0f},{y2:.0f})"


# ---
# Helpers iframes cross-origin (ex. simulateurs web embarqués)
# ---
def _collect_child_frames(node: dict[str, Any], out: list[dict[str, Any]]) -> None:
    for child in node.get("childFrames") or []:
        if f := child.get("frame"):
            out.append(f)
        _collect_child_frames(child, out)


def _find_frame(cdp_fn: Callable[..., Any], frame_match: str = "") -> dict[str, Any]:
    tree = cdp_fn("Page.getFrameTree").get("frameTree") or {}
    frames: list[dict[str, Any]] = []
    _collect_child_frames(tree, frames)
    if not frames:
        raise RuntimeError("Aucune iframe enfant trouvée sur la page.")
    if frame_match:
        needle = frame_match.lower()
        for f in frames:
            if needle in (f.get("url") or "").lower():
                return f
        raise RuntimeError(f"Aucune iframe ne correspond à '{frame_match}'. URLs : {[f.get('url') for f in frames]}")
    return frames[0]


def frame_js(cdp_fn: Callable[..., Any], expression: str, frame_match: str = "") -> Any:
    """Exécute du JavaScript dans une iframe cross-origin via CDP (Page.createIsolatedWorld)."""
    frame = _find_frame(cdp_fn, frame_match)
    ctx = cdp_fn("Page.createIsolatedWorld", frameId=frame["id"], worldName="local_kit")["executionContextId"]
    expr = expression.strip()
    if "return " in expr and not expr.startswith(("(", "function", "async ")):
        expr = f"(() => {{ {expr} }})()"
    res = cdp_fn("Runtime.evaluate", expression=expr, contextId=ctx, returnByValue=True, awaitPromise=True)
    if exc := res.get("exceptionDetails"):
        desc = (exc.get("exception") or {}).get("description") or exc.get("text") or str(exc)
        raise RuntimeError(f"frame_js erreur : {desc}")
    return (res.get("result") or {}).get("value")


def frame_text(cdp_fn: Callable[..., Any], frame_match: str = "") -> str:
    """Retourne le texte visible (innerText) de l'iframe simulateur."""
    return str(frame_js(cdp_fn, "document.body ? document.body.innerText : ''", frame_match) or "")


def _iframe_offset(js_fn: Callable[[str], Any], frame_url_part: str = "") -> tuple[float, float]:
    code = f"""(() => {{
      const part = {json.dumps(frame_url_part.lower())};
      const frames = [...document.querySelectorAll('iframe')];
      const f = (part && frames.find(x => (x.src || '').toLowerCase().includes(part)))
        || frames.find(x => {{ const r = x.getBoundingClientRect(); return r.width > 10 && r.height > 10; }})
        || frames[0];
      if (!f) return null;
      f.scrollIntoView({{block: 'center', inline: 'center', behavior: 'instant'}});
      const r = f.getBoundingClientRect(), cs = getComputedStyle(f);
      return [r.x + f.clientLeft + parseFloat(cs.paddingLeft || '0'), r.y + f.clientTop + parseFloat(cs.paddingTop || '0')];
    }})()"""
    off = js_fn(code)
    if not off:
        raise RuntimeError("Iframe introuvable dans la page parente.")
    return float(off[0]), float(off[1])


def _on_backend_node(cdp_fn: Callable[..., Any], backend_id: int, fn_decl: str, arg: Any = None) -> Any:
    obj = (cdp_fn("DOM.resolveNode", backendNodeId=int(backend_id)).get("object") or {})
    oid = obj.get("objectId")
    if not oid:
        raise RuntimeError(f"Élément [{backend_id}] introuvable (DOM.resolveNode).")
    try:
        r = cdp_fn(
            "Runtime.callFunctionOn",
            objectId=oid,
            functionDeclaration=fn_decl,
            arguments=[{} if arg is None else {"value": arg}],
            returnByValue=True,
            awaitPromise=True,
        )
        if exc := r.get("exceptionDetails"):
            desc = (exc.get("exception") or {}).get("description") or exc.get("text") or str(exc)
            raise RuntimeError(desc)
        return (r.get("result") or {}).get("value")
    finally:
        try:
            cdp_fn("Runtime.releaseObject", objectId=oid)
        except Exception:
            pass


_NODE_INFO_JS = """function() {
  const e = this.nodeType === 1 ? this : this.parentElement;
  e.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const r = e.getBoundingClientRect();
  return {x: r.x + r.width / 2, y: r.y + r.height / 2, tag: e.tagName, type: e.type || '', checked: !!e.checked, value: e.value || ''};
}"""


def frame_click(cdp_fn: Callable[..., Any], js_fn: Callable[[str], Any], backend_id: int,
                frame_url_part: str = "") -> str:
    """Clique de manière fiable un élément `[id]` situé dans une iframe cross-origin (calcul d'offset + fallback)."""
    try:
        cdp_fn("DOM.scrollIntoViewIfNeeded", backendNodeId=int(backend_id))
    except Exception:
        pass
    fx, fy = _iframe_offset(js_fn, frame_url_part)
    info = _on_backend_node(cdp_fn, backend_id, _NODE_INFO_JS)
    x, y = fx + float(info["x"]), fy + float(info["y"])
    cdp_fn("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
    cdp_fn("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)
    time.sleep(0.15)
    after = _on_backend_node(cdp_fn, backend_id, "function(){ const e=this.nodeType===1?this:this.parentElement; return {checked:!!e.checked, value:e.value||''}; }")
    if info["type"] in ("checkbox", "radio") and after["checked"] == info["checked"]:
        _on_backend_node(cdp_fn, backend_id, "function(){ const e=this.nodeType===1?this:this.parentElement; e.click(); }")
        time.sleep(0.1)
        after = _on_backend_node(cdp_fn, backend_id, "function(){ const e=this.nodeType===1?this:this.parentElement; return {checked:!!e.checked, value:e.value||''}; }")
    return f"frame_click [{backend_id}] {info['tag']} ({x:.0f},{y:.0f}) checked={after['checked']}"


def frame_type(cdp_fn: Callable[..., Any], js_fn: Callable[[str], Any], backend_id: int, text: str,
               enter: bool = False, frame_url_part: str = "") -> str:
    """Saisit un texte ou choisit une option <select> pour un élément `[id]` dans une iframe cross-origin."""
    info = _on_backend_node(cdp_fn, backend_id, _NODE_INFO_JS)
    if info["tag"] == "SELECT":
        chosen = _on_backend_node(
            cdp_fn,
            backend_id,
            """function(v){
              const o = [...this.options].find(x => x.value === v || x.text.trim().toLowerCase() === v.toLowerCase());
              if (!o) throw Error('Option introuvable: ' + v);
              this.value = o.value;
              this.dispatchEvent(new Event('input', {bubbles:true}));
              this.dispatchEvent(new Event('change', {bubbles:true}));
              return o.text.trim();
            }""",
            text,
        )
        return f"frame_type [{backend_id}] SELECT = {chosen!r}"
    fx, fy = _iframe_offset(js_fn, frame_url_part)
    x, y = fx + float(info["x"]), fy + float(info["y"])
    cdp_fn("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
    cdp_fn("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)
    try:
        cdp_fn("DOM.focus", backendNodeId=int(backend_id))
    except Exception:
        pass
    sel = {"key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 2}
    cdp_fn("Input.dispatchKeyEvent", type="rawKeyDown", **sel, commands=["selectAll"])
    cdp_fn("Input.dispatchKeyEvent", type="keyUp", **sel)
    if text:
        cdp_fn("Input.insertText", text=text)
    else:
        cdp_fn("Input.dispatchKeyEvent", type="rawKeyDown", key="Backspace", code="Backspace", windowsVirtualKeyCode=8)
        cdp_fn("Input.dispatchKeyEvent", type="keyUp", key="Backspace", code="Backspace", windowsVirtualKeyCode=8)
    actual = _on_backend_node(cdp_fn, backend_id, "function(){ return this.value || ''; }")
    if actual != text:
        _on_backend_node(
            cdp_fn,
            backend_id,
            """function(v){
              const proto = this.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              Object.getOwnPropertyDescriptor(proto, 'value').set.call(this, v);
              this.dispatchEvent(new Event('input', {bubbles:true}));
              this.dispatchEvent(new Event('change', {bubbles:true}));
            }""",
            text,
        )
        actual = text
    if enter:
        cdp_fn("Input.dispatchKeyEvent", type="rawKeyDown", key="Enter", code="Enter", windowsVirtualKeyCode=13)
        cdp_fn("Input.dispatchKeyEvent", type="char", key="Enter", code="Enter", windowsVirtualKeyCode=13, text="\r", unmodifiedText="\r")
        cdp_fn("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter", windowsVirtualKeyCode=13)
    return f"frame_type [{backend_id}] = {actual!r}"
