"""Camada de segmentação documental do NFreader.

Responsabilidades:
- ler páginas individualmente com uma OCR leve quando o PDF não tem texto;
- classificar a página sem depender de um único layout;
- extrair uma assinatura documental (tipo, números, CNPJ, chave, layout);
- agrupar páginas que pertencem ao mesmo documento;
- devolver sub-PDFs para o motor fiscal processar como documentos independentes.

A segmentação é deliberadamente conservadora: um início claro de documento
abre um novo grupo, enquanto uma página sem cabeçalho claro só é anexada à
anterior quando há evidência suficiente de continuidade.
"""
from __future__ import annotations

import io
import re
import unicodedata
import os
import subprocess
import pickle
import tempfile
import sys
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Any
import time

import pdfplumber
import pypdfium2 as pdfium
try:
    import fitz
except Exception:
    fitz = None
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter
import pytesseract

from ocr_engine import HAS_OCR, OCRResult, linguagem_disponivel


STOP_WORDS = {
    "DE", "DA", "DO", "DAS", "DOS", "E", "A", "O", "AS", "OS", "EM", "PARA",
    "COM", "SEM", "POR", "UM", "UMA", "NO", "NA", "NOS", "NAS", "AO", "AOS",
}


def norm(v: Any) -> str:
    s = unicodedata.normalize("NFD", str(v or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().upper()


def compact(v: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", norm(v))


def _extract_cnpjs(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"(?<!\d)(\d{2}[.\s]?\d{3}[.\s]?\d{3}[/\s]?\d{4}[-\s]?\d{2})(?!\d)", text):
        c = re.sub(r"\D", "", m.group(1))
        if len(c) == 14 and c not in out:
            out.append(c)
    return out[:6]


def _extract_access_keys(text: str) -> list[str]:
    out = []

    pattern = re.compile(r"(?<!\d)(?:\d[\s./-]?){44}(?!\d)")
    for raw in pattern.findall(text):
        token = re.sub(r"\D", "", raw)
        if len(token) == 44 and token not in out:
            out.append(token)
    return out[:3]


def _extract_number_candidates(text: str) -> list[str]:
    out: list[str] = []
    u = norm(text)
    patterns = [
        r"(?:N[º°O0]|NUMERO)\s*[:.]?\s*(\d{1,3}(?:[.\s]\d{3})+|\d{1,9})\b",
        r"NF[- ]?E\s*(?:N[º°O0]\s*)?[:.]?\s*(\d{1,3}(?:\.\d{3})+|\d{1,9})\b",
        r"NFS[- ]?E\s*(?:N[º°O0]\s*)?[:.]?\s*(\d{1,3}(?:\.\d{3})+|\d{1,9})\b",
        r"ORCAMENTO\s*(?:NO\.?|N[º°O0])?\s*(\d{1,10})\b",
    ]
    for pat in patterns:
        for m in re.finditer(pat, u):
            v = re.sub(r"\D", "", m.group(1))
            if v and v not in out:
                out.append(v.lstrip("0") or "0")
    return out[:5]


def _header_tokens(text: str) -> set[str]:
    c = compact(text[:max(1400, min(len(text), 5000))])
    tokens: set[str] = set()
    keywords = {
        "DANFE", "DANFSE", "NFE", "NFSE", "NOTAFISCAL", "NOTAFISCALELETRONICA",
        "NOTAFISCALDESERVICOS", "DOCUMENTOAUXILIARDANOTAFISCAL",
        "DOCUMENTOAUXILIARDANFS", "EMITENTE", "PRESTADOR", "TOMADOR",
        "DESTINATARIO", "REMETENTE", "CHAVEDEACESSO", "CODIGODEVERIFICACAO",
        "NUMERO", "SERIE", "SERVICOPRESTADO", "DISCRIMINACAODOSERVICO",
        "DADOSDOSPRODUTOS", "DADOSDOPRODUTO", "DADOSDOPRODUTOSERVICO",
        "VALORTOTALDANOTA", "VALORTOTALDANFSE", "LISTADEEMBARQUE",
        "NOTADEDEBITO", "DACTE", "CTE", "CONHECIMENTODETRANSPORTE",
        "RECIBODOPAGADOR", "LINHADIGITAVEL", "NOSS0NUMERO", "NOSSONUMERO",
    }
    for k in keywords:
        if k in c:
            tokens.add(k)
    return tokens


def _page_classification(text: str) -> tuple[str, float, set[str]]:
    u = norm(text)
    c = compact(text)
    signals: set[str] = set()


    if "LISTADEEMBARQUE" in c:
        return "OUTRO", 98.0, {"LISTADEEMBARQUE"}
    if "NOTADEDEBITO" in c:
        return "OUTRO", 98.0, {"NOTADEDEBITO"}

    nfse = nfe = boleto = cte = 0
    nfse_keys = [
        ("DANFSE", 4), ("NOTAFISCALDESERVICOS", 4), ("NOTA FISCAL DE SERVICOS", 4),
        ("NFS-E", 3), ("PRESTADOR DO SERVICO", 2),
        ("PRESTADOR / FORNECEDOR", 2), ("TOMADOR DO SERVICO", 1),
        ("VALOR TOTAL DA NFS-E", 2), ("VALORTOTALDANFSE", 2),
        ("CODIGO DE VERIFICACAO", 1), ("CODIGODEVERIFICACAO", 1),
        ("DISCRIMINACAO DO SERVICO", 1), ("DISCRIMINACAODOSERVICO", 1),
        ("EMITENTE DA NFS", 3), ("DADOS DA NFS", 2),
        ("SERVICO PRESTADO", 3), ("DETALHAMENTO DO SERVICO", 3),
        ("CALCULO DO ISSQN", 2), ("ISSQN", 1),
    ]
    for key, pts in nfse_keys:
        hit = key in u or key.replace(" ", "") in c
        if hit:
            nfse += pts
            signals.add(key.replace(" ", "_"))


    if re.search(r"(?<![A-Z0-9])NFS\s*[-–]?\s*[E0OC](?![A-Z0-9])", u):
        nfse += 4
        signals.add("NFSE_OCR")
    if re.search(r"(?<![A-Z0-9])DANFS\s*[E0OC](?![A-Z0-9])", u):
        nfse += 5
        signals.add("DANFSE_OCR")

    nfe_keys = [
        ("DANFE", 4), ("NOTA FISCAL ELETRONICA", 4), ("NOTAFISCALELETRONICA", 4),
        ("NF-E", 3), ("CHAVE DE ACESSO", 3), ("CHAVEDEACESSO", 3),
        ("DADOS DOS PRODUTOS", 2), ("DADOSDOSPRODUTOS", 2),
        ("DESTINATARIO / REMETENTE", 1), ("DESTINATARIO/REMETENTE", 1),
        ("NATUREZA DA OPERACAO", 1), ("RECEBEMOS DE", 1),
    ]
    for key, pts in nfe_keys:
        hit = key in u or key.replace(" ", "") in c
        if hit:
            nfe += pts
            signals.add(key.replace(" ", "_"))


    if re.search(r"(?<![A-Z0-9])NF\s*-?\s*E(?![A-Z0-9])", u):
        nfe += 3
        signals.add("NF-E")

    cte_hits=[]
    for key, pts in [
        ("DACTE", 6), ("CT-E", 5),
        ("CONHECIMENTO DE TRANSPORTE", 6), ("DOCUMENTO AUXILIAR DO CONHECIMENTO", 6),
    ]:
        if key in u or key.replace(" ", "") in c:
            cte += pts; cte_hits.append(key); signals.add(key.replace(" ", "_"))


    if not cte_hits:

        estrutural_cte = (
            ("DOCUMENTO" in u and "AUXILIAR" in u and "TRANSPOR" in u and "ELETR" in u)
            or (("DESTE CT" in u or "EMITENTE CT" in u) and "DOCUMENTOS ORIGINARIOS" in u)
        )
        if estrutural_cte and not any(k in u for k in ["NOTA FISCAL", "DANFE", "NF-E"]):
            cte = 6; signals.add("CTE_ESTRUTURAL")
        else:
            cte = 0

    for key, pts in [("RECIBO DO PAGADOR",3),("LINHA DIGITAVEL",3),("NOSSO NUMERO",2),("VALOR DO DOCUMENTO",1),("PAGAVEL EM QUALQUER BANCO",3),("FICHA DE COMPENSACAO",3)]:
        if key in u or key.replace(" ", "") in c:
            boleto += pts; signals.add(key.replace(" ", "_"))

    scores={"NFSE":nfse,"NFE":nfe,"BOLETO":boleto,"CTE":cte}
    best_type,best=max(scores.items(),key=lambda kv:kv[1])
    second=max(v for k,v in scores.items() if k!=best_type)
    if best >= 3 and best >= second + 1:
        conf=min(99.0,55.0+best*6.0+(best-second)*5.0)
        return best_type,conf,signals


    continuation_nfe = (
        ("CODIGO" in u or "CODPRODUTO" in c or "NCM" in u or "CFOP" in u)
        and any(x in c for x in ["QUANTIDADE","VALORUNITARIO","VALORTOTAL","DADOSDOSPRODUTOS","PRODUTO"])
    )
    continuation_nfse = any(x in c for x in [
        "DISCRIMINACAO", "SERVICOPRESTADO", "ISSQN", "RETENCOES", "VALORTOTALDANFSE", "DADOSDANFSE"
    ])
    if continuation_nfse and not continuation_nfe:
        return "NFSE", 56.0, signals | {"CONTINUACAO_NFSE"}
    if continuation_nfe and not continuation_nfse:
        return "NFE", 56.0, signals | {"CONTINUACAO_NFE"}
    return "OUTRO", 30.0 if not u else 42.0, signals


@dataclass
class PageSignature:
    page: int
    tipo: str
    confianca_tipo: float
    texto: str
    cnpjs: list[str]
    chaves: list[str]
    numeros: list[str]
    tokens_cabecalho: list[str]
    linhas: list[str]
    palavras_rodape: list[str]
    visual_profile: list[int] | None = None

    @property
    def primary_id(self) -> tuple[str, str, str, str]:
        return (
            self.tipo,
            self.chaves[0] if self.chaves else "",
            self.numeros[0] if self.numeros else "",
            self.cnpjs[0] if self.cnpjs else "",
        )


def _make_classification_image(image: Image.Image) -> Image.Image:
    """Monta uma imagem pequena com regiões de maior valor para segmentação.

    A etapa de segmentação não precisa ler todos os detalhes do documento. Ela
    precisa de sinais suficientes para dizer "que documento é este?" e "esta
    página parece continuação?". Por isso usamos resolução baixa e regiões
    representativas, deixando o OCR pesado para a etapa fiscal posterior.
    """
    img = ImageOps.autocontrast(ImageOps.grayscale(image))
    w, h = img.size
    pieces = [
        img.crop((0, 0, w, int(h * 0.38))),
        img.crop((0, int(h * 0.42), w, int(h * 0.68))),
        img.crop((0, int(h * 0.82), w, h)),
    ]
    target_w = min(1100, max(x.width for x in pieces))
    resized: list[Image.Image] = []
    for piece in pieces:
        if piece.width != target_w:
            nh = max(1, int(piece.height * target_w / piece.width))
            piece = piece.resize((target_w, nh), Image.Resampling.LANCZOS)
        resized.append(piece)
    gap = 14
    canvas = Image.new("L", (target_w, sum(x.height for x in resized) + gap * 2), 255)
    y = 0
    for piece in resized:
        canvas.paste(piece, (0, y))
        y += piece.height + gap
    return canvas


def _ocr_light(image: Image.Image) -> OCRResult:
    """OCR leve e limitado por passe para nunca deixar uma página prender a fila."""
    if not HAS_OCR:
        return OCRResult("", 0.0, [], [], "indisponivel")

    crop = _make_classification_image(image)
    best: OCRResult | None = None
    lang = linguagem_disponivel()


    for angle in (0,):
        oriented = crop
        variants = [(f"light:r{angle}:psm11", oriented)]
        variants.append((f"light:r{angle}:threshold210", ImageOps.grayscale(oriented).point(lambda p: 255 if p > 210 else 0)))
        for variant, candidate_image in variants:
            try:
                text = pytesseract.image_to_string(candidate_image, lang=lang, config="--oem 3 --psm 11", timeout=5.5)
            except Exception:
                continue
            if not text.strip():
                continue
            _tipo, conf, _ = _page_classification(text)
            candidate = OCRResult(text.strip(), max(35.0, conf), [], [x for x in text.splitlines() if x.strip()], variant)
            if best is None or (candidate.confidence, len(candidate.text)) > (best.confidence, len(best.text)):
                best = candidate
            if conf >= 82 and _tipo in {"NFE", "NFSE", "BOLETO", "CTE"}:
                return candidate
    return best or OCRResult("", 0.0, [], [], "timeout/falha")


def _visual_profile(image: Image.Image) -> list[int]:
    """Assinatura visual compacta para reconhecer páginas do mesmo layout."""
    gray = ImageOps.grayscale(image).resize((20, 28), Image.Resampling.BILINEAR)
    pix = list(gray.getdata())
    return [
        int(sum(pix[i:i + 20]) / max(1, len(pix[i:i + 20])))
        for i in range(0, len(pix), 20)
    ]


def _visual_similarity(a: PageSignature, b: PageSignature) -> float:
    if not a.visual_profile or not b.visual_profile or len(a.visual_profile) != len(b.visual_profile):
        return 0.0
    diffs = [abs(x - y) for x, y in zip(a.visual_profile, b.visual_profile)]
    return max(0.0, 1.0 - (sum(diffs) / max(1, len(diffs)) / 255.0))


def _render_page_image(page_bytes: bytes, dpi: int) -> Image.Image:
    """Renderiza uma página isolada e fecha imediatamente o documento."""
    if fitz is not None:
        doc = fitz.open(stream=page_bytes, filetype="pdf")
        try:
            page = doc[0]
            scale = dpi / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        finally:
            doc.close()
    doc = pdfium.PdfDocument(page_bytes)
    try:
        return doc[0].render(scale=dpi / 72.0).to_pil()
    finally:
        doc.close()


def _render_page_from_pdf_path(pdf_path: str, page_index: int, dpi: int) -> Image.Image:
    """Renderiza somente uma página do PDF original dentro do processo isolado."""
    if fitz is not None:
        doc = fitz.open(pdf_path)
        try:
            page = doc[page_index]
            scale = dpi / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        finally:
            doc.close()
    doc = pdfium.PdfDocument(pdf_path)
    try:
        return doc[page_index].render(scale=dpi / 72.0).to_pil()
    finally:
        doc.close()


def _build_signature(text: str, index: int, visual: list[int] | None = None, tipo_override: tuple[str, float] | None = None) -> PageSignature:
    text = (text or "").strip()
    if tipo_override is None:
        tipo, conf, _signals = _page_classification(text)
    else:
        tipo, conf = tipo_override
        _signals = set()
    return PageSignature(
        page=index + 1,
        tipo=tipo,
        confianca_tipo=round(conf, 1),
        texto=text,
        cnpjs=_extract_cnpjs(text),
        chaves=_extract_access_keys(text),
        numeros=_extract_number_candidates(text),
        tokens_cabecalho=sorted(_header_tokens(text)),
        linhas=[x.strip() for x in text.splitlines() if x.strip()][:100],
        palavras_rodape=[x.strip() for x in text.splitlines()[-14:] if x.strip()],
        visual_profile=visual,
    )


def _analyze_one_page(page_bytes: bytes, native_text: str, index: int, dpi: int) -> PageSignature:
    """Compatibilidade para chamadas antigas que já possuem os bytes da página."""
    text = (native_text or "").strip()
    tipo, conf, _signals = _page_classification(text)
    visual = None
    try:


        needs_ocr = HAS_OCR and (len(text) < 220 or conf < 65.0)
        img = _render_page_image(page_bytes, 96 if needs_ocr else 72)
        visual = _visual_profile(img)
        if needs_ocr:
            result = _ocr_light(img)
            if result.text.strip():
                text = f"{text}\n{result.text}" if text else result.text
                tipo, conf, _signals = _page_classification(text)
    except Exception:
        pass
    return _build_signature(text, index, visual, (tipo, conf))


def _analyze_one_page_from_pdf(pdf_path: str, native_text: str, index: int, dpi: int) -> PageSignature:
    """Analisa uma única página. Toda chamada ocorre em processo isolado."""
    text = (native_text or "").strip()
    tipo, conf, _signals = _page_classification(text)
    visual = None


    needs_ocr = HAS_OCR and (len(text) < 220 or conf < 65.0)
    render_dpi = 96 if needs_ocr else 60
    img = _render_page_from_pdf_path(pdf_path, index, render_dpi)
    try:
        visual = _visual_profile(img)
    except Exception:
        visual = None

    if needs_ocr:
        result = _ocr_light(img)
        if result.text.strip():
            text = f"{text}\n{result.text}" if text else result.text
            tipo, conf, _signals = _page_classification(text)


        if tipo == "OUTRO" and conf < 65.0:
            img_hi = _render_page_from_pdf_path(pdf_path, index, 140)
            result2 = _ocr_light(img_hi)
            if result2.text.strip():
                text = f"{text}\n{result2.text}" if text else result2.text
                tipo, conf, _signals = _page_classification(text)

    return _build_signature(text, index, visual, (tipo, conf))


def _fallback_signature(native_text: str, index: int) -> PageSignature:
    return _build_signature(native_text, index)


def _read_native_pages(pdf_bytes: bytes) -> list[str]:
    if fitz is not None:
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            try:
                return [p.get_text("text") or "" for p in doc]
            finally:
                doc.close()
        except Exception:
            pass
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return [p.extract_text(x_tolerance=2, y_tolerance=3) or "" for p in pdf.pages]
    except Exception:
        return []


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """Encerra também eventuais processos descendentes do worker."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _spawn_page_segment_worker(pdf_path: str, native_text: str, index: int, dpi: int) -> dict[str, Any]:
    worker = Path(__file__).with_name("segment_worker.py")
    td = tempfile.TemporaryDirectory(prefix="nfreader_seg_page_")
    inp = Path(td.name) / "input.pkl"
    out = Path(td.name) / "output.pkl"
    inp.write_bytes(pickle.dumps((pdf_path, native_text, index, dpi), protocol=pickle.HIGHEST_PROTOCOL))
    env = os.environ.copy()
    env.update({
        "OMP_NUM_THREADS": "1",
        "OMP_THREAD_LIMIT": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "PYTHONUNBUFFERED": "1",
    })
    kwargs: dict[str, Any] = {
        "cwd": str(worker.parent),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    proc = subprocess.Popen([sys.executable, str(worker), str(inp), str(out)], **kwargs)
    return {
        "proc": proc,
        "out": out,
        "td": td,
        "deadline": time.monotonic() + float(os.getenv("NFREADER_SEGMENT_PAGE_TIMEOUT", "30")),
        "index": index,
    }


def _finish_page_segment_worker(state: dict[str, Any]) -> PageSignature:
    proc = state["proc"]
    out = state["out"]
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError("Falha no worker de segmentação da página.")
    value = pickle.loads(out.read_bytes())
    if not isinstance(value, PageSignature):
        raise RuntimeError("Worker não retornou uma assinatura de página válida.")
    return value


def _run_isolated_page(task: tuple[str, str, int, int]) -> PageSignature:
    pdf_path, native_text, index, dpi = task
    state = _spawn_page_segment_worker(pdf_path, native_text, index, dpi)
    proc = state["proc"]
    try:
        while proc.poll() is None:
            if time.monotonic() >= state["deadline"]:
                _terminate_process_tree(proc)
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
                raise TimeoutError(f"OCR de segmentação excedeu o limite na página {index + 1}.")
            time.sleep(0.05)
        return _finish_page_segment_worker(state)
    finally:
        try:
            if proc.poll() is None:
                _terminate_process_tree(proc)
            proc.wait(timeout=2)
        except Exception:
            pass
        try:
            state["td"].cleanup()
        except Exception:
            pass


def _native_signature_is_enough(text: str) -> bool:
    """Detecta páginas digitais que podem ser segmentadas sem OCR."""
    text = (text or "").strip()
    if len(text) < 220:
        return False
    tipo, conf, _ = _page_classification(text)
    if tipo in {"NFE", "NFSE", "OUTRO", "BOLETO", "CTE"} and conf >= 65.0:
        return True
    return any(k in compact(text) for k in ("DANFE", "DANFSE", "LISTADEEMBARQUE", "NOTADEDEBITO"))


def analyze_pdf_pages(pdf_bytes: bytes, dpi: int = 100, progress_callback=None) -> list[PageSignature]:
    """Analisa páginas em paralelo controlado, isolando cada OCR.

    O desenho evita dois problemas típicos de PDFs longos:
    1) um Tesseract preso não pode bloquear as páginas seguintes;
    2) vários Tesseracts não compartilham o mesmo processo Python por dezenas
       de páginas, reduzindo vazamento de memória e acumulação de estado.
    """
    native = _read_native_pages(pdf_bytes)
    total = len(native)
    if total == 0:
        return []
    if len(native) < total:
        native.extend([""] * (total - len(native)))

    results: list[PageSignature | None] = [None] * total
    pending: list[tuple[str, str, int, int]] = []


    with tempfile.TemporaryDirectory(prefix="nfreader_seg_pdf_") as td:
        pdf_path = str(Path(td) / "document.pdf")
        Path(pdf_path).write_bytes(pdf_bytes)

        for index, text in enumerate(native):
            if _native_signature_is_enough(text):
                results[index] = _build_signature(text, index)
                if progress_callback:
                    try:
                        done = sum(x is not None for x in results)
                        progress_callback(done, total, f"Identificando página {index + 1} de {total}...")
                    except Exception:
                        pass
            else:
                pending.append((pdf_path, text, index, dpi))

        if pending:
            try:
                workers = max(1, min(3, int(os.getenv("NFREADER_SEGMENT_WORKERS", "2"))))
            except Exception:
                workers = 2


            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {executor.submit(_run_isolated_page, task): task[2] for task in pending}
                for future in as_completed(future_map):
                    index = future_map[future]
                    try:
                        results[index] = future.result()
                    except Exception:
                        results[index] = _fallback_signature(native[index], index)
                    if progress_callback:
                        try:
                            done = sum(x is not None for x in results)
                            progress_callback(done, total, f"Identificando página {index + 1} de {total}...")
                        except Exception:
                            pass

    return [x for x in results if x is not None]


def _is_strong_start(sig: PageSignature) -> bool:
    t = sig.tokens_cabecalho
    return any(k in t for k in {
        "DANFE", "DANFSE", "NFE", "NFSE", "NOTAFISCALDESERVICOS", "CHAVEDEACESSO", "CODIGODEVERIFICACAO",
        "LISTADEEMBARQUE", "NOTADEDEBITO"
    }) or bool(sig.chaves)


def _same_set(a: list[str], b: list[str]) -> bool:
    return bool(set(a) & set(b))


def _layout_similarity(a: PageSignature, b: PageSignature) -> float:
    A, B = set(a.tokens_cabecalho), set(b.tokens_cabecalho)
    if not A or not B:
        return 0.0
    return len(A & B) / max(1, len(A | B))


def _has_continuation_content(sig: PageSignature, tipo: str) -> bool:
    u = compact(sig.texto)
    if tipo == "NFE":
        return any(k in u for k in ["DADOSDOSPRODUTOS", "CODIGOPRODUTO", "NCM", "CFOP", "VALORUNITARIO", "QUANTIDADE"]) or "CONTINUACAO_NFE" in sig.tokens_cabecalho
    if tipo == "NFSE":
        return any(k in u for k in ["DISCRIMINACAODOSERVICO", "SERVICOPRESTADO", "ISSQN", "VALORDAOPERACAO", "RETENCOES"]) or "CONTINUACAO_NFSE" in sig.tokens_cabecalho
    return len(compact(sig.texto)) > 100


def _continuation_score(prev: PageSignature, cur: PageSignature) -> tuple[float, list[str]]:
    reasons: list[str] = []
    if prev.tipo == "OUTRO" and cur.tipo == "OUTRO":
        score = 0.0
    elif prev.tipo != cur.tipo:
        return -999.0, []
    else:
        score = 0.0


    if prev.chaves and cur.chaves and _same_set(prev.chaves, cur.chaves):
        score += 80; reasons.append("mesma chave de acesso")
    if prev.numeros and cur.numeros and _same_set(prev.numeros, cur.numeros):
        score += 36; reasons.append("mesmo número")
    if prev.cnpjs and cur.cnpjs and _same_set(prev.cnpjs, cur.cnpjs):
        score += 24; reasons.append("mesmo CNPJ")

    layout = _layout_similarity(prev, cur)
    score += layout * 16
    if layout >= 0.55:
        reasons.append("layout semelhante")

    if _has_continuation_content(cur, prev.tipo):
        score += 18
        reasons.append("conteúdo compatível com continuação")


    if _is_strong_start(cur):
        if not ((prev.chaves and _same_set(prev.chaves, cur.chaves)) or (prev.numeros and _same_set(prev.numeros, cur.numeros) and prev.cnpjs and _same_set(prev.cnpjs, cur.cnpjs))):
            score -= 55
            reasons.append("novo cabeçalho detectado")
    return score, reasons


def _text_similarity(a: PageSignature, b: PageSignature) -> float:
    def toks(sig: PageSignature) -> set[str]:
        words = re.findall(r"[A-Z0-9]{3,}", norm(sig.texto))
        return {w for w in words if w not in STOP_WORDS}
    A, B = toks(a), toks(b)
    if not A or not B:
        return 0.0
    return len(A & B) / max(1, len(A | B))


def _same_identity(a: PageSignature, b: PageSignature) -> bool:
    if a.tipo != b.tipo:
        return False
    if a.chaves and b.chaves and _same_set(a.chaves,b.chaves):
        return True
    if a.numeros and b.numeros and _same_set(a.numeros,b.numeros) and a.cnpjs and b.cnpjs:
        return _same_set(a.cnpjs,b.cnpjs)
    return False


def _outro_same_family(a: PageSignature, b: PageSignature) -> bool:
    common=set(a.tokens_cabecalho)&set(b.tokens_cabecalho)
    if "LISTADEEMBARQUE" in common:
        return True
    visual=_visual_similarity(a,b)
    text=_text_similarity(a,b)
    return len(common)>=1 and visual>=0.84 and text>=0.10


def _should_attach_unknown_page(prev: PageSignature, cur: PageSignature) -> bool:
    if prev.tipo not in {"NFE","NFSE","OUTRO","BOLETO","CTE"}:
        return False
    if cur.tipo=="OUTRO" and prev.tipo=="OUTRO":


        if _is_strong_start(cur):
            return False
        if _outro_same_family(prev,cur):
            return True
        if not _is_strong_start(cur) and (_visual_similarity(prev,cur)>=0.93 or _text_similarity(prev,cur)>=0.25):
            return True
        return False
    if cur.tipo != prev.tipo:
        return False
    if _same_identity(prev,cur):
        return True
    if _is_strong_start(cur):
        return False
    text_sim=max(_layout_similarity(prev,cur),_text_similarity(prev,cur))
    visual_sim=_visual_similarity(prev,cur)
    continuation=_has_continuation_content(cur,prev.tipo)
    if prev.tipo in {"NFE","NFSE"} and visual_sim>=0.88:
        return continuation or len(compact(cur.texto))<100
    return continuation and text_sim>=0.18


def group_pages(signatures: list[PageSignature]) -> list[list[PageSignature]]:
    if not signatures:
        return []
    groups: list[list[PageSignature]]=[[signatures[0]]]
    for cur in signatures[1:]:
        current=groups[-1]
        prev=current[-1]


        if _same_identity(prev,cur):
            current.append(cur); continue

        if _should_attach_unknown_page(prev,cur):
            current.append(cur); continue

        if prev.tipo != cur.tipo and cur.tipo != "OUTRO":
            groups.append([cur]); continue

        score,reasons=_continuation_score(prev,cur)
        attach_threshold=54.0 if cur.tipo in {"NFE","NFSE"} else 28.0

        if cur.tipo in {"NFE","NFSE"} and _is_strong_start(cur):


            groups.append([cur]); continue

        if score>=attach_threshold:
            current.append(cur)
        else:
            groups.append([cur])
    return groups


def _subset_pdf(pdf_bytes: bytes, pages_1based: list[int]) -> bytes:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for page_no in pages_1based:
        idx = page_no - 1
        if 0 <= idx < len(reader.pages):
            writer.add_page(reader.pages[idx])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def segment_pdf(pdf_bytes: bytes, nome_arquivo: str, progress_callback=None) -> list[dict]:
    signatures = analyze_pdf_pages(pdf_bytes, progress_callback=progress_callback)
    groups = group_pages(signatures)
    out: list[dict] = []
    for doc_index, group in enumerate(groups, 1):
        pages = [s.page for s in group]
        tipo_votes: dict[str, float] = {}
        for s in group:
            tipo_votes[s.tipo] = tipo_votes.get(s.tipo, 0.0) + s.confianca_tipo
        tipo = max(tipo_votes, key=tipo_votes.get) if tipo_votes else "OUTRO"


        if tipo == "OUTRO" and any(s.tipo in {"NFE", "NFSE"} for s in group):
            tipo = max((s.tipo for s in group if s.tipo in {"NFE", "NFSE"}), key=lambda t: tipo_votes.get(t, 0.0))
        range_label = str(pages[0]) if len(pages) == 1 else f"{pages[0]}–{pages[-1]}"
        rotulo = f"{nome_arquivo} (página {range_label})"
        try:
            subset = _subset_pdf(pdf_bytes, pages)
        except Exception:
            subset = pdf_bytes
        confidence = round(min(99.0, sum(s.confianca_tipo for s in group) / max(1, len(group))), 1)
        out.append({
            "index": doc_index,
            "pages": pages,
            "tipo": tipo,
            "confianca": confidence,
            "arquivo": rotulo,
            "source_filename": nome_arquivo,
            "bytes": subset,
            "signatures": [asdict(s) for s in group],
        })
    return out
