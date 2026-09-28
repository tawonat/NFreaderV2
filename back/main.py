from __future__ import annotations

import asyncio
import io
import re
import zipfile
import unicodedata
import os
from concurrent.futures import ThreadPoolExecutor
import uuid
import time
from urllib.parse import quote
from pathlib import Path
from typing import Any
from functools import lru_cache

from pydantic import BaseModel

import pandas as pd
import pdfplumber
import pypdfium2 as pdfium
from fastapi import FastAPI, File, Form, UploadFile, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, Response
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pypdf import PdfReader, PdfWriter
from rapidfuzz import fuzz, process
from PIL import Image, ImageOps
import pytesseract

from ocr_engine import HAS_OCR, OCRWord, executar_passe, extract as ocr_extract, linguagem_disponivel
from document_segmentation import segment_pdf, _subset_pdf
from gemini_engine import GeminiError, analyze_fiscal_pdf, analyze_fiscal_pdfs
import auth

BASE_DIR = Path(__file__).resolve().parent
CADASTRO_DIR = BASE_DIR / "cadastro"
PRODUTOS_XLSX = CADASTRO_DIR / "produtos_totvs.xlsx"
SERVICOS_XLSX = CADASTRO_DIR / "servicos_totvs.xlsx"

app = FastAPI()


_login_failures: dict[str, list[float]] = {}

@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.method == "OPTIONS":
        return Response(status_code=405)
    if request.url.path == "/auth/login":
        return await call_next(request)
    user = auth.current_user(request.cookies.get(auth.COOKIE))
    if not user:
        return JSONResponse({"detail": "Faça login para continuar."}, status_code=401)
    request.state.username = user
    return await call_next(request)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
async def login(credentials: LoginRequest, request: Request):
    address = request.client.host if request.client else "unknown"
    now = time.time()
    failures = [t for t in _login_failures.get(address, []) if now - t < 900]
    _login_failures[address] = failures
    if len(failures) >= 10:
        raise HTTPException(status_code=429, detail="Muitas tentativas. Aguarde 15 minutos.")
    if not auth.authenticate(credentials.username, credentials.password):
        failures.append(now)
        raise HTTPException(status_code=401, detail="Usuário ou senha inválidos.")
    _login_failures.pop(address, None)
    response = JSONResponse({"username": credentials.username})
    response.set_cookie(auth.COOKIE, auth.create_session(credentials.username), max_age=auth.SESSION_SECONDS,
                        httponly=True, secure=os.getenv("NFREADER_SECURE_COOKIE", "0") == "1", samesite="lax", path="/")
    return response


@app.get("/auth/me")
async def me(request: Request):
    return {"username": request.state.username}


@app.post("/auth/logout")
async def logout(request: Request):
    auth.revoke_session(request.cookies.get(auth.COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response

REVISION_SESSIONS: dict[str, dict] = {}
ANALYSIS_JOBS: dict[str, dict] = {}
SESSION_TTL_SECONDS = 6 * 60 * 60

class ExportRequest(BaseModel):
    session_id: str
    centro_custo: str = ""
    nfe_rows: list[dict] = []
    nfse_rows: list[dict] = []
    cte_rows: list[dict] = []
    outros_rows: list[dict] = []
    orcamento_rows: list[dict] = []

def _limpar_sessoes_expiradas() -> None:
    agora = time.time()
    for sid in [k for k,v in REVISION_SESSIONS.items() if agora - v.get("created_at", agora) > SESSION_TTL_SECONDS]:
        REVISION_SESSIONS.pop(sid, None)

def _sanitizar_rows(rows: list[dict], *, para_excel: bool) -> list[dict]:


    internos = {"id", "document_id", "candidatos", "confiancas", "Confiança", "_score", "_revisar", "_document_id", "_tipo", "_id", "_candidatos", "_confiancas", "Total da Peça Num"}
    if para_excel:
        internos.update({"Arquivo", "Observações"})
    return [
        {
            k: v
            for k, v in row.items()
            if not str(k).startswith("_") and k not in internos
        }
        for row in rows
    ]


def _sanitizar_export_rows(rows: list[dict]) -> list[dict]:
    return _sanitizar_rows(rows, para_excel=True)


def _sanitizar_review_rows(rows: list[dict]) -> list[dict]:
    return _sanitizar_rows(rows, para_excel=False)

CNPJ_RE = re.compile(r"(?<!\d)\d{2}[.\s]?\d{3}[.\s]?\d{3}[/\s]?\d{4}[-\s]?\d{2}(?!\d)")
DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
NCM_RE = re.compile(r"(?<!\d)(\d{7,8})(?!\d)")
MONEY_RE = re.compile(r"(?<![\d/])(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2,4}(?!\d)")
UNIDADES = {"UN", "UND", "UNID", "PC", "PÇ", "KG", "G", "L", "LT", "M", "M2", "M3", "CX", "FD", "SC", "HR", "H", "JG", "PAR", "TON", "RL", "ML", "SV"}


def normalizar_texto(v: Any) -> str:
    s = str(v or "")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().upper()


def normalizar_descricao(v: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", normalizar_texto(v))).strip()


def parse_num(v: str | None) -> float | None:
    if not v:
        return None
    s = str(v).strip().replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def format_num(v: float | None, decimals: int = 3) -> str:
    if v is None:
        return "-"
    s = f"{v:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s.rstrip("0").rstrip(",") if "," in s else s


def clean_company(v: str) -> str:
    """
    Limpa uma razão social sem cortar palavras legítimas.

    Regra importante: NÃO procurar "ME"/"SA" no meio da frase, porque isso
    pode truncar empresas como "COMERCIO" ou "MATERIAL". O nome só é
    considerado encerrado por sufixo societário quando o sufixo aparece no
    final do candidato.
    """
    s = re.sub(r"\s+", " ", str(v or "")).strip(" :-|_[]()\\")
    s = re.split(r"(?i)\b(?:E-?MAIL|EMAIL)\b", s)[0]
    s = re.split(r"(?i)\b(?:CNPJ|CPF)\b", s)[0]
    s = s.strip(" :-|_[]()")


    suffix = re.search(r"(?:LTDA|EIRELI|EPP|MEI|(?<![A-Z])ME|S\s*\.?\s*A\.?|S/A)[.,]?\s*$", s, re.I)
    if suffix:
        return s

    return s


def rows_from_words(words: list[OCRWord]) -> list[dict]:
    """Agrupa palavras por posição vertical, não apenas pelo line_num do Tesseract."""
    words = sorted(words, key=lambda w: (w.cy, w.left))
    rows: list[dict] = []
    for w in words:
        if not rows or abs(w.cy - rows[-1]["cy"]) > max(13, w.height * 0.9):
            rows.append({"cy": w.cy, "top": w.top, "bottom": w.top + w.height, "words": [w]})
        else:
            r = rows[-1]
            r["words"].append(w)
            r["cy"] = sum(x.cy for x in r["words"]) / len(r["words"])
            r["top"] = min(r["top"], w.top)
            r["bottom"] = max(r["bottom"], w.top + w.height)
    out = []
    for r in rows:
        ws = sorted(r["words"], key=lambda x: x.left)
        out.append({"cy": r["cy"], "top": r["top"], "bottom": r["bottom"], "text": " ".join(x.text for x in ws), "words": ws})
    return out


def _process_one_pdf_page(page_bytes: bytes, native: str) -> dict:
    text = native.strip()
    ocr_used = False
    conf = 0.0
    words: list[OCRWord] = []
    image = None
    error = ""
    try:

        un = normalizar_texto(text)
        structured_digital = any(k in un for k in [
            "DANFSE", "VALOR TOTAL DA NFS-E", "PRESTADOR / FORNECEDOR",
            "RECIBO DO PAGADOR", "COMPROVANTE DE ENTREGA", "NOSSO NUMERO",
            "DACTE", "CONHECIMENTO DE TRANSPORTE"
        ])
        possible_nfe = any(k in un for k in ["DANFE", "NF-E", "RECEBEMOS DE", "DESTINATARIO / REMETENTE"])
        needs_ocr = HAS_OCR and (
            len(text) < 250
            or (possible_nfe and not any(k in un for k in ["DADOS DOS PRODUTOS", "DADOS DO PRODUTO", "DADOS DO PRODUTOS / SERVICOS", "DADOS DO PRODUTO / SERVICO"]) and not structured_digital)
        )
        if needs_ocr:
            doc = pdfium.PdfDocument(page_bytes)
            image = doc[0].render(scale=300 / 72).to_pil()
            doc.close()
            r = ocr_extract(image, detalhar=True, timeout=float(os.getenv("NFREADER_NORMAL_OCR_TIMEOUT", "32")))
            if getattr(r, "rotation", 0):
                image = image.rotate(r.rotation, expand=True, fillcolor="white")
            conf = r.confidence
            words = r.words
            ocr_used = True


            ru=normalizar_texto(r.text)
            if any(k in ru for k in ["DANFSE", "NFS-E", "NOTA FISCAL DE SERVICOS"]):
                try:
                    header=image.crop((0,0,image.width,int(image.height*.38)))
                    hr=executar_passe(header,"--oem 3 --psm 11",timeout=float(os.getenv("NFREADER_HEADER_OCR_TIMEOUT","9")))
                    existing={normalizar_texto(x) for x in r.lines}
                    recovered=[]
                    for line in hr.lines:
                        if normalizar_texto(line) not in existing:
                            recovered.append(line); existing.add(normalizar_texto(line))
                    r.lines=recovered+r.lines
                    r.text="\n".join(r.lines)
                    words.extend(hr.words)
                except Exception:
                    pass
            if len(text) < 300:
                text = r.text
            else:
                existing = {normalizar_texto(x) for x in text.splitlines()}
                merged = text.splitlines()
                for line in r.lines:
                    if normalizar_texto(line) not in existing:
                        merged.append(line)
                text = "\n".join(merged)
    except Exception as e:
        error = f"Falha no OCR: {e}"
    rows = rows_from_words(words)
    return {"text": text, "lines": text.splitlines(), "native": native.splitlines(), "ocr": ocr_used, "conf": conf, "words": words, "rows": rows, "image": image, "error": error}


def _split_pdf_page_bytes(pdf_bytes: bytes) -> list[bytes]:
    try:
        reader=PdfReader(io.BytesIO(pdf_bytes))
        pages=[]
        for page in reader.pages:
            writer=PdfWriter(); writer.add_page(page); buf=io.BytesIO(); writer.write(buf); pages.append(buf.getvalue())
        return pages or [pdf_bytes]
    except Exception:
        return [pdf_bytes]


def process_pdf_pages(pdf_bytes: bytes) -> list[dict]:
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            natives = [p.extract_text() or "" for p in pdf.pages]
    except Exception as e:
        return [{"text": "", "lines": [], "native": [], "ocr": False, "conf": 0, "words": [], "rows": [], "image": None, "error": f"PDF inválido: {e}"}]

    page_bytes=_split_pdf_page_bytes(pdf_bytes)
    total=len(page_bytes)
    if len(natives)<total:
        natives.extend([""]*(total-len(natives)))
    try:
        workers=max(1,min(2,int(os.getenv("NFREADER_OCR_WORKERS","2"))))
    except Exception:
        workers=2
    tasks=[(page_bytes[i],natives[i]) for i in range(total)]
    if total==1 or workers==1:
        return [_process_one_pdf_page(pb,native) for pb,native in tasks]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures=[executor.submit(_process_one_pdf_page,pb,native) for pb,native in tasks]
        return [f.result() for f in futures]


def classify(text: str) -> str:
    u = normalizar_texto(text)
    if any(x in u for x in ["DACTE", "CT-E", "DOCUMENTO AUXILIAR DO CONHECIMENTO DE TRANSPORTE"]):
        return "CTE"
    nfse = sum(x in u for x in ["DANFSE", "NFS-E", "PRESTADOR / FORNECEDOR", "PRESTADOR DO SERVICO", "VALOR TOTAL DA NFS-E"])
    boleto = sum(x in u for x in ["RECIBO DO PAGADOR", "COMPROVANTE DE ENTREGA", "NOSSO NUMERO", "VALOR DO DOCUMENTO", "LINHA DIGITAVEL"])
    nfe = sum(x in u for x in ["DANFE", "NATUREZA DA OPERACAO", "DADOS DOS PRODUTOS", "NF-E", "RECEBEMOS DE"])
    if nfse >= 1 and nfse >= max(boleto, nfe):
        return "NFSE"
    if boleto >= 2 and boleto > nfe:
        return "BOLETO"
    if nfe >= 1:
        return "NFE"
    return "OUTRO"


def extract_cte(lines: list[str]) -> dict:
    """Extrai somente campos seguros do CT-e; o serviço TOTVS é regra fixa."""
    numero = "SemNumero"
    empresa = "Empresa não identificada"

    for line in lines:
        normalized = normalizar_texto(line)
        match = re.search(
            r"(?:CT-?E|CONHECIMENTO(?:\s+DE\s+TRANSPORTE)?)\s*(?:N[º°O.]*)?\s*[:.-]?\s*(\d{1,12})\b",
            normalized,
        )
        if match:
            numero = str(int(match.group(1)))
            break

    for index, line in enumerate(lines):
        normalized = normalizar_texto(line)
        if normalized in {"EMITENTE", "DADOS DO EMITENTE", "REMETENTE"}:
            for candidate in lines[index + 1:index + 5]:
                cleaned = clean_company(candidate)
                if _parece_razao_social(cleaned):
                    empresa = cleaned
                    break
        if empresa != "Empresa não identificada":
            break

    return {"numero": numero, "empresa": empresa}


def build_cte_row(
    lines: list[str],
    cadastro_servicos: dict,
    centro_custo: str,
    arquivo: str,
    document_id: str,
    confidence: float,
) -> dict:
    """Cria a linha revisável do CT-e com o código obrigatório 780007."""
    extracted = extract_cte(lines)
    service = next(
        (row for row in cadastro_servicos["rows"] if str(row.get("codigo", "")).strip() == "780007"),
        None,
    )
    if service is None:
        raise RuntimeError("O cadastro de serviços não contém o código obrigatório 780007 para CT-e.")

    code_confidence = 100.0
    base_confidence = max(0.0, min(100.0, float(confidence or 0.0)))
    number_confidence = 95.0 if extracted["numero"] != "SemNumero" else 35.0
    company_confidence = 90.0 if extracted["empresa"] != "Empresa não identificada" else 35.0
    return {
        "Número da Nota": extracted["numero"],
        "Empresa": extracted["empresa"],
        "Descrição na Nota": "Serviço de transporte (CT-e)",
        "Quantidade": "1",
        "Código no Banco de Dados": "780007",
        "Descrição no Banco de Dados": str(service.get("descricao", "SERVICO TRANSPORTE")),
        "Centro de Custos": centro_custo,
        "Confiança": "100%",
        "Observações": "CT-e/DACTE identificado; código TOTVS definido pela regra de negócio.",
        "Arquivo": arquivo,
        "_id": f"cte-{uuid.uuid4().hex[:10]}",
        "_document_id": document_id,
        "_tipo": "CT-E",
        "_candidatos": [],
        "_confiancas": {
            "Número da Nota": number_confidence,
            "Empresa": company_confidence,
            "Descrição na Nota": max(85.0, base_confidence),
            "Quantidade": 100.0,
            "Código no Banco de Dados": code_confidence,
            "Descrição no Banco de Dados": code_confidence,
            "Centro de Custos": 100.0,
            "Observações": 100.0,
        },
    }


def extract_nfe_emitter(lines: list[str], image: Image.Image | None) -> str:
    """Extrai o emitente da NF-e priorizando âncoras legais e o bloco do DANFE."""
    full = "\n".join(lines)
    patterns = [

        r"RECEBEMOS\s+DE\s+(.+?)\s+(?:OS\s+PRODUTOS(?:\s+E/?OU\s+SERVI[CÇ]OS)?|OS\s+SERVI[CÇ]OS)\s+CONSTANTES",
        r"RECEBEMOS\s+DE\s+(.+?)\s+OS\s+PRODUTOS\s+E?/\?OU\s+SERVI[CÇ]OS\s+CONSTANTES",
        r"RECEBEMOS\s+DE\s+(.+?)(?:\s+OS\s+PRODUTOS|\s+OS\s+SERVI[CÇ]OS)",
    ]
    for pat in patterns:
        m = re.search(pat, full, re.I | re.S)
        if m:
            cand = clean_company(m.group(1))
            if len(normalizar_descricao(cand)) >= 4:
                return cand


    for line in lines[:45]:
        cand=_candidato_empresa_nfse(line)
        nc=normalizar_texto(cand)
        if _parece_razao_social(cand) and not any(k in nc for k in [
            "DESTINATARIO", "REMETENTE", "TRANSPORTADOR", "DOCUMENTO AUXILIAR",
            "NOTA FISCAL", "SERIE", "CONTROLE DO FISCO",
        ]):
            return cand


    for i, line in enumerate(lines[:40]):
        if "DANFE" not in normalizar_texto(line):
            continue
        before = re.split(r"(?i)\bDANFE\b", line, maxsplit=1)[0].strip(" :-|_[]()")
        before = clean_company(before)
        nb = normalizar_descricao(before)
        if len(nb) >= 4 and not any(k in nb for k in ["DOCUMENTO AUXILIAR", "NOTA FISCAL", "CONTROLE DO FISCO"]):
            return before


        for j in (i - 1, i + 1):
            if 0 <= j < len(lines):
                cand = clean_company(lines[j])
                nc = normalizar_descricao(cand)
                if len(nc) >= 4 and not any(k in nc for k in ["DOCUMENTO AUXILIAR", "NOTA FISCAL", "CHAVE DE ACESSO", "CONTROLE DO FISCO", "SERIE", "IDENTIFICACAO DO EMITENTE"]):
                    return cand

    if image:
        try:
            top = image.crop((0, 0, image.width, int(image.height * .28)))
            txt = pytesseract.image_to_string(top, lang=linguagem_disponivel(), config="--oem 3 --psm 6")
            for line in txt.splitlines():
                m = re.search(r"RECEBEMOS\s+DE\s+(.+?)(?:\s+OS\s+PRODUTOS|\s+OS\s+SERVI[CÇ]OS)", line, re.I)
                if m:
                    cand = clean_company(m.group(1))
                    if len(normalizar_descricao(cand)) >= 4:
                        return cand
                if "DANFE" in normalizar_texto(line):
                    cand = clean_company(re.split(r"(?i)\bDANFE\b", line, 1)[0])
                    if len(normalizar_descricao(cand)) >= 4:
                        return cand
        except Exception:
            pass
    return ""

def extract_nfe_number(lines: list[str]) -> str:
    """Extrai o número completo da NF-e, inclusive formatos 000.335.546."""
    for line in lines[:55]:
        u = normalizar_texto(line)

        m = re.search(r"(?:N[º°]|N[O0]\b)\s*[:.-]?\s*(\d{1,3}(?:[.\s]\d{3})+|\d{1,9})\b", u)
        if m:
            valor = re.sub(r"\D", "", m.group(1))
            if valor:
                return valor.lstrip("0") or "0"

        m = re.search(r"\bNF[- ]?E?\s*(?:N[º°:]?\s*)?(\d{1,3}(?:[.]\d{3})+|\d{1,9})\b", u)
        if m and "CNPJ" not in u:
            valor = re.sub(r"\D", "", m.group(1))
            if valor:
                return valor.lstrip("0") or "0"
    return "SemNumero"

def _numeric_candidates(text: str) -> list[str]:
    vals = []
    for x in re.findall(r"(?<!\d)(?:\d{1,3}(?:\.\d{3})+|\d+)(?:[.,]\d{1,4})?(?!\d)", text):
        if parse_num(x) is not None:
            vals.append(x)
    return vals


def _find_nfe_header_centers(page: dict) -> dict:
    """Encontra o centro das colunas da tabela DANFE usando as palavras do cabeçalho.
    A leitura da quantidade nunca depende apenas da ordem dos tokens do OCR.
    """
    centers: dict[str, float] = {}
    rows = page.get("rows", [])
    if not rows:
        return centers

    labels = {
        "quantidade": ("QUANT", "QTDE", "QTD", "QUANTIDADE"),
        "unitario": ("V UNIT", "VUNIT", "UNIT"),
        "total": ("V TOTAL", "VTOTAL", "TOTAL"),
        "unidade": ("UNID", "UN", "UND"),
    }


    for row in rows:
        ws = sorted(row["words"], key=lambda w: w.left)
        txt = normalizar_texto(row["text"])
        if not any(k in txt for k in ["QUANT", "QTDE", "QTD"]):
            continue
        for i, w in enumerate(ws):
            u = normalizar_texto(w.text).replace(".", "")
            if any(k in u for k in labels["quantidade"]):
                centers["quantidade"] = w.cx
            elif any(k in u for k in labels["unidade"]):
                centers.setdefault("unidade", w.cx)

            nxt = " ".join(normalizar_texto(x.text).replace(".", "") for x in ws[i:i+2])
            if "V UNIT" in nxt or "VUNIT" in nxt or "UNIT" == u:
                centers["unitario"] = (ws[i].cx + ws[min(i+1, len(ws)-1)].cx) / 2
            if "V TOTAL" in nxt or "VTOTAL" in nxt:
                centers["total"] = (ws[i].cx + ws[min(i+1, len(ws)-1)].cx) / 2
        if len(centers) >= 3:
            break


    if "quantidade" not in centers:
        for row in rows:
            for w in row["words"]:
                u = normalizar_texto(w.text).replace(".", "")
                if any(k in u for k in labels["quantidade"]):
                    centers["quantidade"] = w.cx
                    break
            if "quantidade" in centers:
                break


    return centers


def _ocr_column_number(image: Image.Image, x_center: float, y0: int, y1: int, width_ratio: float = 0.045) -> str:
    if image is None or x_center is None:
        return ""
    x0 = max(0, int(x_center - image.width * width_ratio / 2))
    x1 = min(image.width, int(x_center + image.width * width_ratio / 2))
    crop = image.crop((x0, max(0, y0 - 12), x1, min(image.height, y1 + 12))).convert("L")
    crop = ImageOps.autocontrast(crop)
    crop = crop.resize((max(220, crop.width * 8), max(80, crop.height * 8)))
    candidatos = []
    for psm in (7, 6, 11, 13):
        try:
            raw = pytesseract.image_to_string(
                crop, lang=linguagem_disponivel(),
                config=f"--oem 3 --psm {psm} -c tessedit_char_whitelist=0123456789,.",
                timeout=float(os.getenv("NFREADER_COLUMN_OCR_TIMEOUT", "4")),
            ).strip()
        except Exception:
            continue
        for token in _numeric_candidates(raw):
            n = parse_num(token)
            if n is None or n <= 0:
                continue
            score = 100 + (5 if re.search(r"[,.]\d", token) else 0)
            candidatos.append((score, token))
    return sorted(candidatos, reverse=True)[0][1] if candidatos else ""


def _numeric_tokens_between(words, x_left: float, x_right: float):
    vals = []
    for w in sorted(words, key=lambda x: x.left):
        if w.cx < x_left or w.cx > x_right:
            continue
        for token in _numeric_candidates(w.text):
            n = parse_num(token)
            if n is not None and n > 0:
                vals.append((w, token, n))
    return vals


def _quantity_from_row_words(page: dict, row: dict, headers: dict, unit_center: float | None = None) -> str:
    """Lê QUANTIDADE isolando a célula entre as linhas/centros das colunas.
    Isso evita capturar V.TOTAL, que é o erro mais comum nos scans.
    """
    image = page.get("image")
    if image is None:
        return ""

    qx = headers.get("quantidade")
    ux = headers.get("unitario")
    tx = headers.get("total")


    if qx is not None:
        left = (unit_center + qx) / 2 if unit_center is not None else max(0, qx - image.width * 0.045)
        right_candidates = [x for x in (ux, tx) if x is not None and x > qx]
        right = (qx + min(right_candidates)) / 2 if right_candidates else qx + image.width * 0.035
        vals = _numeric_tokens_between(row.get("words", []), left, right)
        valid = []
        for w, token, n in vals:
            if 0 < n <= 100000000:
                score = w.confidence + 20
                if re.search(r"[,.]\d{1,4}$", token):
                    score += 12

                if unit_center is not None and w.cx <= unit_center:
                    score -= 25
                valid.append((score, token))
        if valid:
            return sorted(valid, reverse=True)[0][1]


        if ux is not None:
            return _ocr_column_number(image, qx, row["top"], row["bottom"], 0.04)


    if unit_center is not None:
        vals = []
        for w, token, n in _numeric_tokens_between(row.get("words", []), unit_center + image.width * 0.015, image.width * 0.86):


            if n <= 0 or n > 100000000:
                continue
            score = w.confidence
            if re.search(r"[,.]\d{1,4}$", token):
                score += 8
            score -= max(0, (w.cx - unit_center) / image.width) * 20
            vals.append((w.cx, score, token, n))
        if vals:
            vals.sort(key=lambda x: x[0])

            for _, _, token, n in vals:
                if n > 0.0001:
                    return token

    return ""

def extract_nfe_items(page: dict) -> list[dict]:
    """Extrai itens da DANFE. Para scans, a quantidade é lida pela coluna QUANTIDADE."""
    if not page["ocr"]:
        return extract_nfe_native(page["lines"])
    out = []
    seen = set()
    rows = page.get("rows", [])
    headers = _find_nfe_header_centers(page)
    in_table = False
    for row in rows:
        u = normalizar_texto(row["text"])
        if "DADOS DOS PRODUTOS" in u or ("COD" in u and "PRODUTO" in u and "QUANT" in u):
            in_table = True
            continue
        if in_table and any(k in u for k in ["DADOS ADICIONAIS", "INFORMACOES COMPLEMENTARES"]):
            in_table = False
            continue
        if not in_table:

            if not re.match(r"^\d{4,}\s", u):
                continue
        ws = sorted(row["words"], key=lambda x: x.left)
        if not ws:
            continue
        code_i = None
        code = ""
        for i, w in enumerate(ws[:8]):
            digits = re.sub(r"\D", "", w.text)
            if len(digits) >= 4:
                code_i, code = i, digits
                break
        if code_i is None:
            continue

        unit_i = None
        unit = ""
        for i, w in enumerate(ws[code_i + 1:], start=code_i + 1):
            t = re.sub(r"[^A-Za-zÀ-ÿ]", "", w.text).upper()
            if t in UNIDADES:
                unit_i, unit = i, t
                break
        if unit_i is None:
            continue
        parts = []
        for w in ws[code_i + 1:unit_i]:
            t = w.text.strip("[]()|_")
            if not t:
                continue
            digits = re.sub(r"\D", "", t)

            if digits and not re.search(r"[A-Za-zÀ-ÿ]", t) and len(digits) >= 4:
                continue
            parts.append(t)
        desc = re.sub(r"\s+", " ", " ".join(parts)).strip(" -")

        desc = re.sub(r"(?:\s+|^)(?:0\d\d|\d{3,4})$", "", desc).strip(" -")
        if len(desc) < 3:
            continue
        unit_center = ws[unit_i].cx if unit_i is not None else None
        quantidade = _quantity_from_row_words(page, row, headers, unit_center)


        ncm = ""
        for w in ws[code_i + 1:unit_i]:
            digits = re.sub(r"\D", "", w.text)
            if len(digits) == 8:
                ncm = digits
                break
        qnum = parse_num(quantidade)
        if qnum is None or qnum <= 0:
            quantidade = ""
        key = (code, normalizar_descricao(desc))
        if key in seen:
            continue
        seen.add(key)
        q_vals = _numeric_tokens_between(row.get("words", []), max(0, (unit_center or 0) - page["image"].width * 0.02), min(page["image"].width, (unit_center or 0) + page["image"].width * 0.16))
        q_conf = float(max(55.0, min(99.0, max([w.confidence for w, _, _ in q_vals] or [float(page.get("conf", 70.0))]))))
        out.append({"codigo_fornecedor": code, "descricao": desc, "ncm": ncm, "un": unit, "quantidade": quantidade, "confianca_quantidade": q_conf})
    return out


def extract_nfe_native(lines: list[str]) -> list[dict]:
    """Parser robusto para DANFE digitais: extrai código, descrição, NCM, unidade e quantidade."""
    out = []
    in_table = False
    item_re = re.compile(
        r"^(?P<codigo>[^\s]+)\s+(?P<desc>.*?)\s+"
        r"(?P<ncm>\d{8}|\d{4}\.\d{2}\.\d{2})\s+"
        r"(?P<trib>[A-Za-z0-9/.\-]+)\s+(?P<cfop>\d{4})\s+"
        r"(?P<un>UN|UND|UNID|PC|PÇ|KG|G|L|LT|M|M2|M3|CX|FD|SC|HR|H|JG|PAR|TON|RL|ML|SV)\s+"
        r"(?P<qtd>\d+(?:[.,]\d+)?)\b",
        re.I,
    )
    for i, line in enumerate(lines):
        u = normalizar_texto(line)
        if "DADOS DOS PRODUTOS" in u or "DADOS DO PRODUTO" in u:
            in_table = True
            continue
        if in_table and any(k in u for k in ["DADOS ADICIONAIS", "INFORMACOES COMPLEMENTARES"]):
            break
        if not in_table:
            continue
        m = item_re.match(line.strip())
        if not m:
            continue
        qtd = m.group("qtd")
        if parse_num(qtd) is None or parse_num(qtd) <= 0:
            qtd = ""
        out.append({
            "codigo_fornecedor": m.group("codigo"),
            "descricao": m.group("desc").strip(" -"),
            "ncm": re.sub(r"\D", "", m.group("ncm")),
            "un": m.group("un").upper(),
            "quantidade": qtd,
            "confianca_quantidade": 98.0,
        })
    return out

def _compactar_rotulo(v: str) -> str:
    """Normaliza rótulos do DANFSe removendo espaços artificiais do PDF."""
    return re.sub(r"[^A-Z0-9]", "", normalizar_texto(v))


def _eh_descricao_generica_nfse(desc: str) -> bool:
    n = normalizar_descricao(desc)
    return n in {
        "PRESTACAO DE SERVICO",
        "SERVICO PRESTADO",
        "SERVICOS PRESTADOS",
        "SERVICO",
        "SERVICOS",
        "PRESTACAO",
        "DESCRICAO SERVICOS",
        "DESCRICAO DOS SERVICOS",
    }


def _parece_razao_social(v: str) -> bool:
    u = normalizar_texto(v).strip()
    return bool(
        u
        and re.search(r"(?:LTDA|(?<![A-Z])S\.?A\.?|(?<![A-Z])S/A|EIRELI|MEI|(?<![A-Z])ME|EPP)[.,]?$", u, re.I)
        and len(u) >= 4
    )


_SUFIXO_EMPRESA_RE = re.compile(
    r"(?i)([A-ZÀ-Ý0-9&.'\-/ ]{3,}?(?:LTDA|EIRELI|EPP|(?<![A-ZÀ-Ý])S\s*\.?A\s*\.?|(?<![A-ZÀ-Ý])S/A|MEI|(?<![A-ZÀ-Ý])ME))[.,]?(?=\s|$)"
)


def _candidato_empresa_nfse(raw: str) -> str:
    """Extrai uma razão social de uma linha OCR, rejeitando endereço/rótulo."""
    s = re.sub(r"\s+", " ", str(raw or "")).strip(" :-|_")
    s = re.sub(r"^(?:[O0]?\d[\d.\-/]*\d)\s+(?=[A-Za-zÀ-ÿ])", "", s, flags=re.I)
    if not s:
        return ""

    s = re.split(r"(?i)\s+(?:E-?MAIL|EMAIL|CNPJ|CPF|IM|INSCRI[CÇ][AÃ]O)\s*:?", s)[0].strip()
    m = _SUFIXO_EMPRESA_RE.search(s)
    if m:
        s = m.group(1).strip()
        s = re.sub(r"^(?:[O0]?\d[\d.\-/() ]*\d)\s+(?=[A-Za-zÀ-ÿ])", "", s, flags=re.I)
    c = clean_company(s)
    u = normalizar_texto(c)
    rejeitar = (
        "RAZAO SOCIAL", "NOME EMPRESARIAL", "NOME FANTASIA", "PRESTADOR",
        "TOMADOR", "SIMPLES NACIONAL", "DATA DE COMPETENCIA", "REGIME",
        "ENDERECO", "MUNICIPIO", "TRAVESSA", "AVENIDA", "RODOVIA", "CEP",
        "INSCRI", "TELEFONE", "CPF", "CNPJ", "E-MAIL", "EMAIL", "MUNICI",
        "CHAVE DE ACESSO", "PORTAL NACIONAL", "SITUACAO DA NFS", "NFS-E MEI",
    )
    if len(u) < 4 or DATE_RE.search(c) or any(x in u for x in rejeitar):
        return ""

    if not _parece_razao_social(c):
        words = re.findall(r"[A-ZÀ-Ý]{2,}", u)
        if len(words) < 2 or re.search(r"@|\bRUA\b|\bPR\b\s*\d{5}", u):
            return ""
    return c


def _linha_descritiva_servico(s: str) -> str:
    """Retorna uma linha limpa de descrição da área SERVIÇO PRESTADO ou ''."""
    if not s or len(normalizar_descricao(s)) < 5:
        return ""
    raw=re.sub(r"\s+"," ",s).strip()
    u=_compactar_rotulo(raw)
    if "RETIDO" in u and "VALORTOTA" in u:
        return ""
    if any(k in u for k in ["SERVICOPRESTADO","DESCRICAODOSERVICO","DESCRICAOSERVICOS","CODIGODETRIBUTACAO","CODIGODANBS","LOCALDAPRESTACAO","TRIBUTACAOMUNICIPAL","TRIBUTACAOFEDERAL","TRIBUTACAOIBSCBS","VALORTOTAL","VALORDAOPERACAO","BCISSQN","ALIQUOTAAPLICADA","MUNICIPIODAINCIDENCIA","RESPONSAVELPELORECOLHIMENTO","EXIGIBILIDADEDOISS","VALORIMPOST","RETIDOALIQ"]):
        return ""


    raw2 = re.split(r"\s+\d{3,}(?:[./]\d+)*\s*/", raw, maxsplit=1)[0].strip()
    if re.fullmatch(r"[\d./\-\s]+",raw2):
        return ""
    if re.search(r"(?i)\b(?:CEP|CNPJ|CPF|E-MAIL|EMAIL|MUNICIPIO|SIGLA UF)\b",raw2):
        return ""


    raw2=re.split(r"(?i)\s+R\$\s*[\d.,]+",raw2,maxsplit=1)[0].strip()
    raw2=re.split(r"\s+(?:SIM|NAO|NÃO)\s+\d+[,.]\d+\s+\d",raw2,maxsplit=1,flags=re.I)[0].strip()

    if re.match(r"^\s*(?:\d{2}(?:[./]\d{2}){1,3}|\d{3,}(?:[./]\d+)*)(?:\s*/|\s+-|\s+\d)",raw2):
        return ""
    letters=len(re.sub(r"[^A-Za-zÀ-ÿ]","",raw2))
    digits=len(re.sub(r"[^0-9]","",raw2))
    if letters < 5 or (digits > letters*2 and digits >= 8):
        return ""
    return raw2.strip(" |-")


def _extrair_servico_prestado_completo(lines: list[str]) -> tuple[str,str]:
    compact=[_compactar_rotulo(x) for x in lines]
    inicio=next((i for i,u in enumerate(compact) if any(k in u for k in [
        "SERVICOPRESTADO", "DETALHAMENTODOSERVICO", "DISCRIMINACAODOSSERVICOS",
        "DISCRIMINACAODOSERVICO", "DESCRICAODOSSERVICOS", "SERVICOSPRESTADOS",
    ]) or (u.startswith("DETALHA") and "SERVI" in u)),None)
    if inicio is None: return "",""
    fim=len(lines)
    for i in range(inicio+1,len(lines)):
        if re.match(r"^TRIBUTA[CG]AO",compact[i]) or any(k in compact[i] for k in [
            "TRIBUTACAOMUNICIPAL", "TRIBUTACAOFEDERAL", "CALCULODOISSQN", "CALCULODOISS",
            "VALORTOTALDANFSE", "VALORDAOPERACAOSERVICO", "VALORTOTALDANOTA",
            "VALORTOTALDOSSERVICOS", "DEDUCOES", "RETENCOESFEDERAIS",
            "INFORMACOESCOMPLEMENTARES", "OUTRASINFORMACOES", "MUNICIPIODAINCIDENCIA",
            "RESPONSAVELPELORECOLHIMENTO", "EXIGIBILIDADEDOISS",
            "DANFSE", "DOCUMENTOAUXILIAR", "CHAVEDEACESSO", "EMITENTEDANFSE",
        ]):
            fim=i; break
    desc_idx = inicio if any(k in compact[inicio] for k in [
        "DETALHAMENTODOSERVICO", "DISCRIMINACAODOSSERVICOS", "DISCRIMINACAODOSERVICO",
        "DESCRICAODOSSERVICOS", "SERVICOSPRESTADOS",
    ]) else next((i for i in range(inicio+1,fim) if any(k in compact[i] for k in [
        "DESCRICAODOSERVICO", "DESCRICAODOSSERVICOS", "DESCRICAOSERVICOS",
        "DISCRIMINACAODOSERVICO", "DISCRIMINACAODOSSERVICOS",
    ]) or (compact[i].startswith("DESCRI") and "SERVI" in compact[i])),None)
    before=[]; after=[]
    for i in range(inicio+1,fim):
        if desc_idx is not None and i>desc_idx:
            cleaned=_linha_descritiva_servico(lines[i])
            if cleaned: after.append(cleaned)
        elif desc_idx is None:
            cleaned=_linha_descritiva_servico(lines[i])
            if cleaned: before.append(cleaned)
        else:
            cleaned=_linha_descritiva_servico(lines[i])
            if cleaned: before.append(cleaned)
    def dedupe(parts):
        out=[]
        for x in parts:
            nx=normalizar_descricao(x)
            if nx and all(nx!=normalizar_descricao(y) for y in out): out.append(x)
        return out
    before,after=dedupe(before),dedupe(after)
    after = [x for x in after if not _eh_descricao_generica_nfse(x)]


    completo=" ".join(after if after else before).strip()
    return completo," ".join(after).strip()

def _extrair_ordem_servico(lines: list[str]) -> dict|None:
    norm_full=normalizar_texto("\n".join(lines))
    if "PRESTACAO DE SERVICOS" not in norm_full or "SERVICOS ALOCADOS" not in norm_full: return None
    numero="SemNumero"
    m=re.search(r"PRESTACAO\s+DE\s+SERVICOS\s+N[º°oO0]?\s*:?\s*(\d{3,12})",norm_full)
    if m: numero=str(int(m.group(1)))
    empresa=""
    for line in lines[:15]:
        s=re.sub(r"\s+"," ",line).strip()
        m=re.search(r"^(.*?)\s+Emiss[aã]o\s*:",s,re.I)
        if m and len(m.group(1))>=5: empresa=m.group(1).strip(" :-"); break
    inicio=next((i for i,x in enumerate(lines) if "SERVICOSALOCADOS" in _compactar_rotulo(x)),None)
    descricoes=[]; qtd_total=0.0
    if inicio is not None:
        for raw in lines[inicio+1:]:
            s=re.sub(r"\s+"," ",raw).strip()
            cu=_compactar_rotulo(s)
            if "TOTALDOSSERVICOS" in cu or "TOTAISDAOS" in cu: break
            mrow=re.match(r"^\d{2}/\d{2}/\d{4}\s+\d+\s+(.*?)\s+(\d+(?:[.,]\d+)?)\s+[\d.]+,\d{2}\s+[\d.]+,\d{2}\s+[\d.]+,\d{2}\s+[\d.]+,\d{2}\s*$",s,re.I)
            if mrow:
                descricoes.append(mrow.group(1).strip()); qtd_total += parse_num(mrow.group(2)) or 1.0
    valor="-"
    for raw in lines:
        m=re.search(r"(?i)VALOR\s+TOTAL\s+DA\s+OS\s*:\s*R?\$?\s*([\d.]+,\d{2,4})",raw)
        if m: valor=m.group(1); break
    if valor=="-":
        for raw in lines:
            m=re.search(r"(?i)TOTAL\s+DOS\s+SERVI[CÇ]OS\.*\s*:?\s*\d+\s+([\d.]+,\d{2,4})",raw)
            if m: valor=m.group(1); break
    desc="; ".join(dict.fromkeys(descricoes)) or "Prestacao de servico"
    return {"numero":numero,"empresa":empresa or "Empresa não identificada","descricao":desc,"descricao_match":desc,"contexto_servico":desc,"quantidade":qtd_total if qtd_total>0 else 1.0,"valor_total":valor,"tipo_documento":"ORDEM_SERVICO"}


def _valor_liquido_por_posicao(rows: list[dict] | None) -> str:
    """Lê o montante abaixo da coluna 'Valor Líquido' usando coordenadas OCR."""
    if not rows:
        return ""
    for row in rows:
        if "VALOR LIQUIDO" not in normalizar_texto(row.get("text", "")):
            continue
        words=row.get("words", [])
        target=next((w for w in words if "LIQUID" in normalizar_texto(w.text)), None)
        if target is None:
            continue
        candidates=[]
        for other in rows:
            dy=float(other.get("cy",0))-float(row.get("cy",0))
            if dy < -10 or dy > 180:
                continue
            for w in other.get("words",[]):
                token=w.text.strip("R$|:;()[]")
                if re.fullmatch(r"\d{1,3}(?:\.\d{3})*,\d{2}",token) and abs(w.cx-target.cx) <= 280:
                    candidates.append((max(0,dy),abs(w.cx-target.cx),token))
        if candidates:
            return min(candidates,key=lambda x:(x[0],x[1]))[2]
    return ""


def extract_nfse(lines: list[str], rows: list[dict] | None = None) -> dict:
    os_doc = _extrair_ordem_servico(lines)
    if os_doc:
        return os_doc
    norm = [normalizar_texto(x) for x in lines]
    compact = [_compactar_rotulo(x) for x in lines]

    numero = "SemNumero"
    for i, u in enumerate(compact):
        if "NUMERODANFSE" in u or "MERODANFSE" in u:
            for x in lines[i:i+5]:

                m = re.search(r"(?<![/\d])(\d{1,8})\s+(?:\d{2}/\d{2}/\d{4}|\d{2}/\d{4})", x)
                if m and int(m.group(1)) <= 99999999:
                    numero = str(int(m.group(1)))
                    break

                m = re.search(r"(?<![0-9A-Za-z])(\d{2,}\.\d{2,})(?=\s+\d{2}/(?:\d{2}/)?\d{4})", x)
                if m:
                    digits = re.sub(r"\D", "", m.group(1))
                    numero = str(int(digits)) if digits else "SemNumero"
                    break
            if numero == "SemNumero":
                for x in lines[i+1:i+5]:
                    token = re.match(r"\s*([0-9.]{2,})\s+", x)
                    if token and re.search(r"\d", token.group(1)):
                        digits = re.sub(r"\D", "", token.group(1))
                        if digits:
                            numero = str(int(digits))
                            break
            if numero == "SemNumero":
                puros=[]
                for x in lines[i+1:i+11]:
                    m = re.fullmatch(r"\s*(\d{1,9})\s*", x)
                    if m and int(m.group(1)) > 0:
                        puros.append(m.group(1))
                escolhido=next((x for x in puros if len(x)>=2), puros[0] if puros else "")
                if escolhido:
                    numero=str(int(escolhido))
            break

    if numero == "SemNumero" or len(re.sub(r"\D","",numero)) <= 1:


        for raw in lines[:55]:
            digits=re.sub(r"\D","",raw)
            if len(digits)==50:
                chave_num=digits[23:36]
                if chave_num.isdigit() and int(chave_num)>0:
                    numero=str(int(chave_num))
                    break
        if numero == "SemNumero" or len(re.sub(r"\D","",numero)) <= 1:
            for i,u in enumerate(compact[:45]):
                if "CHAVEDEACESSO" not in u:
                    continue
                joined="".join(re.sub(r"\D","",x) for x in lines[i+1:i+7])
                if len(joined)>=50:
                    chave_num=joined[:50][23:36]
                    if chave_num.isdigit() and int(chave_num)>0:
                        numero=str(int(chave_num))
                        break

    if numero == "SemNumero":


        for raw in lines[:90]:
            m = re.search(r"(?<!\d)(\d{3,9})\s*/\s*([A-Z])(?![A-Z])", normalizar_texto(raw))
            if m:
                numero = str(int(m.group(1)))
                break

    if numero == "SemNumero":


        rotulos_numero = (
            "NUMERODANOTAFISCAL", "NUMERODANOTA", "NUMERODANFSE",
            "NODANFSE", "NFDANFSE", "NOTAFISCALN", "WIMERODANOTAFISCAL",
            "MIMERODANOTAFISCAL", "NMERODANOTAFISCAL", "NOMERODANOTAFISCAL",
            "MERODANOTAFISCAL",
        )
        for i, u in enumerate(compact[:35]):
            if not any(k in u for k in rotulos_numero):
                continue
            trecho = " ".join(lines[i:min(len(lines), i + 3)])
            trecho = re.sub(r"\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?", " ", trecho)
            m = re.search(
                r"(?i)(?:N[ÚU]MERO\s+DA\s+(?:NFS-?E|NOTA(?:\s+FISCAL)?)|N[º°O]\s+DA\s+(?:NFS-?E|NOTA)|N[ÚU]MERO|N[º°O]|NOTA(?:\s+FISCAL)?\s*N[º°O]?)\s*[:.\-]?\s*(\d{1,9})\b",
                trecho,
            )
            if not m:
                puros=[]
                for candidate in lines[i + 1:min(len(lines), i + 11)]:
                    pure = re.match(r"\s*(\d{1,9})\s*$", candidate)
                    if pure: puros.append(pure.group(1))
                escolhido=next((x for x in puros if len(x)>=2), puros[0] if puros else "")
                m=re.match(r"(\d+)",escolhido) if escolhido else None
            if m and int(m.group(1)) > 0:
                numero = str(int(m.group(1)))
                break

    empresa = ""

    prestador_pos=next((i for i,u in enumerate(compact) if any(k in u for k in [
        "EMITENTEDANFSE","PRESTADORFORNECEDOR","PRESTADORDESERVICO","DADOSDOPRESTADOR"
    ])),0)
    tomador_pos=next((i for i,u in enumerate(compact[prestador_pos+1:],prestador_pos+1) if "TOMADOR" in u),min(len(lines),prestador_pos+40))
    for raw in lines[prestador_pos:tomador_pos]:
        m=re.search(r"(?i)NOME\s*/?\s*RAZ\S*\s+SOCI\S*\s*[:|-]?\s*(.+)$",raw)
        if m:
            empresa=_candidato_empresa_nfse(m.group(1))
            if empresa: break
    if not empresa:


        inicio = next((i for i,u in enumerate(compact) if any(k in u for k in [
            "PRESTADORDESERVICOS", "DADOSDOPRESTADOR", "EMITENTEDANFSE",
            "PRESTADORFORNECEDOR", "PRESTADOR",
        ])), None)
        if inicio is not None:
            fim = min(len(lines), inicio + 24)
            for j in range(inicio + 1, fim):
                if any(k in compact[j] for k in ["TOMADORDOSERVICOS", "DADOSDOTOMADOR", "TOMADORADQUIRENTE"]):
                    fim = j
                    break

            indices = []
            for j in range(inicio, fim):
                if (
                    any(k in compact[j] for k in ["RAZAOSOCIAL", "NOMEEMPRESARIAL", "NOMEDOPRESTADOR"])
                    or ("NOME" in compact[j] and any(k in compact[j] for k in ["RAZ", "SOCIAL", "EMPRES"]))
                ):
                    indices.extend(range(j + 1, min(fim, j + 4)))
            labelled = set(indices)


            for j in range(inicio+1,fim):
                for raw in (lines[j], " ".join(lines[j:min(fim,j+2)])):
                    candidato=_candidato_empresa_nfse(raw)
                    if _parece_razao_social(candidato):
                        if len(re.findall(r"[A-ZÀ-Ý]{2,}",normalizar_texto(candidato))) <= 2:
                            for k in range(j-1,max(inicio,j-8),-1):
                                prefixo=_candidato_empresa_nfse(lines[k])
                                if prefixo and not _parece_razao_social(prefixo) and len(re.findall(r"[A-ZÀ-Ý]{2,}",normalizar_texto(prefixo)))>=2:
                                    combinado=_candidato_empresa_nfse(prefixo+" "+candidato)
                                    if _parece_razao_social(combinado): candidato=combinado
                                    break
                        empresa=candidato; break
                if empresa: break


            indices.extend(range(inicio + 1, fim))
            for j in dict.fromkeys(indices) if not empresa else []:
                candidato = _candidato_empresa_nfse(lines[j])
                empresa = candidato if (j in labelled or _parece_razao_social(candidato)) else ""
                if empresa:
                    break

    desc = ""
    contexto = ""
    desc_idx = None
    for i,u in enumerate(compact):
        if any(k in u for k in ["DESCRICAODOSERVICO", "DESCRICAODOSSERVICOS", "DISCRIMINACAODOSERVICO", "DISCRIMINACAODOSSERVICOS", "DETALHAMENTODOSERVICO"]) or (u.startswith("DETALHA") and "SERVI" in u):
            desc_idx=i
            vals=[]
            for x in lines[i+1:i+8]:
                xu=_compactar_rotulo(x)
                if not x.strip(): continue
                if any(marker in xu for marker in ["TRIBUTACAOMUNICIPAL","TRIBUTACAOFEDERAL","TRIBUTACAOIBSCBS","VALORTOTALDANFSE","VALORDAOPERACAOSERVICO"]): break
                vals.append(x.strip())
                if len(" ".join(vals))>=250: break
            desc=re.sub(r"\s+"," "," ".join(vals)).strip()
            break

    servico_completo, desc_explicita = _extrair_servico_prestado_completo(lines)
    if servico_completo:


        desc = servico_completo

    if not desc:
        full="\n".join(lines)
        m=re.search(r"Descri(?:ç|c)ã?o\s*do\s*Servi(?:ç|c)o\s*[:\-]?\s*(.+?)(?=\n(?:TRIBUTA|VALOR TOTAL|VALOR DA OPERA)|$)",full,re.I|re.S)
        if m: desc=re.sub(r"\s+"," ",m.group(1)).strip()
    if not desc:


        for raw in lines:
            u=normalizar_texto(raw)
            if "SERVICOS" not in u or len(re.findall(r"[A-ZÀ-Ý]",u))<12:
                continue
            cleaned=re.split(r"(?i)\s+(?:SIM|N[AÃA]O)\s+\d+[,.]\d+\s+\d",raw,maxsplit=1)[0].strip(" |-_")
            if len(normalizar_descricao(cleaned))>=12 and "NOTA FISCAL" not in normalizar_texto(cleaned):
                desc=cleaned
                break
    if not desc: desc="Prestacao de servico"
    if re.search(r"(?i)\bRETIDO\b",desc) and re.search(r"(?i)VALOR\s+TOTA",desc):
        desc=re.sub(r"(?is)^.*?VALOR\s+TOTA(?:L)?\s+", "", desc, count=1).strip()
    desc=re.sub(r"(?i)^servi[cç]o(?:s)?de", "serviço de ", desc)
    desc=re.sub(r"\s+"," ",desc).strip()
    contexto=servico_completo or desc
    descricao_para_match=contexto

    valor = "-"
    valor_posicional = _valor_liquido_por_posicao(rows)
    if valor_posicional:
        valor = valor_posicional

    if valor == "-":
        for raw in lines:
            m = re.search(r"(?i)(?:VLR\s+LIQ(?:UIDO)?(?:\s+DUP)?|VALOR\s+LIQUIDO)\s*[:=]?\s*(?:R\$\s*)?([\d.]+[,.]\d{2})", raw)
            if m:
                parsed=parse_num(m.group(1))
                if parsed is not None:
                    valor=format_num(parsed,2)
                    break
    if valor == "-":
        for raw in lines:
            if "SERVICOS" not in normalizar_texto(raw):
                continue
            valores=re.findall(r"(?<!\d)(\d{1,3}(?:\.\d{3})*,\d{2})(?!\d)",raw)
            if valores:
                valor=valores[-1]
                break
    for i, u in enumerate(compact):
        if valor != "-": break
        if "VALORDAOPERACAOSERVICO" in u or "VALORDOSERVICO" in u:
            m = re.search(r"R\$\s*([\d.]+,\d{2,4})", " ".join(lines[i:i+7]))
            if m:
                valor = m.group(1)
                break
    if valor == "-":
        for i, u in enumerate(compact):
            if "VALORTOTALDANFSE" in u:
                m = re.search(r"R\$\s*([\d.]+,\d{2,4})", " ".join(lines[i:i+10]))
                if m:
                    valor = m.group(1)
                    break
    if valor == "-":


        inicio_total = next((i for i,u in enumerate(compact) if "VALORTOTALDANOTA" in u), None)
        if inicio_total is not None:
            fim_total = next((i for i in range(inicio_total + 1, len(lines)) if "INFORMACOESCOMPLEMENTARES" in compact[i]), min(len(lines), inicio_total + 18))
            valores=[]
            for raw in lines[inicio_total + 1:fim_total]:
                for token in re.findall(r"(?<!\d)(?:R\$\s*)?(\d{1,3}(?:\.\d{3})*,\d{2})(?!\d)", raw, re.I):
                    valores.append(token)
            if valores:
                valor=valores[-1]

    full = normalizar_texto(" ".join(lines))
    quantidade = 1.0
    hour_patterns = [
        r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:HORAS|HORA|HRS|HR)(?![A-Z])",
        r"QUANTIDADE\s*[:=]?\s*(\d+(?:[.,]\d+)?)",
        r"(\d+(?:[.,]\d+)?)\s*(?:H|HH)\b",
    ]
    for pat in hour_patterns:
        m = re.search(pat, full)
        if m:
            quantidade = parse_num(m.group(1)) or 1.0
            break

    return {
        "numero": numero,
        "empresa": empresa or "Empresa não identificada",
        "descricao": desc,
        "descricao_match": descricao_para_match,
        "contexto_servico": contexto,
        "quantidade": quantidade,
        "valor_total": valor,
    }


def carregar_cadastro(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Cadastro fixo não encontrado: {path.name}")
    raw = pd.read_excel(path, header=0, dtype=object)
    raw = raw.dropna(how="all").reset_index(drop=True)
    if raw.empty:
        raise ValueError(f"Cadastro vazio: {path.name}")
    return raw


def escolher_colunas_cadastro(df: pd.DataFrame) -> tuple[str, str, str | None]:
    keys = {c: normalizar_descricao(c) for c in df.columns}
    cod = next((c for c, k in keys.items() if k == "CODIGO" or k.endswith(" CODIGO")), None)
    if not cod:
        cod = next((c for c, k in keys.items() if "CODIGO" in k and "BARRAS" not in k), None)
    desc_short = next((c for c, k in keys.items() if k == "DESCRICAO"), None)
    desc_cols = [c for c, k in keys.items() if k.startswith("DESCRICAO")]
    desc = desc_short or (desc_cols[0] if desc_cols else None)
    desc_long = desc_cols[1] if len(desc_cols) > 1 else None
    if not cod or not desc:
        raise ValueError(f"Cadastro {Path(df.__class__.__name__).name if False else ''}: colunas necessárias não encontradas. Encontradas: {', '.join(map(str, df.columns))}")
    return cod, desc, desc_long


@lru_cache(maxsize=4)
def cadastro_preparado(path_str: str) -> dict:
    path=Path(path_str)
    df = carregar_cadastro(path)
    cod_col, desc_col, desc2_col = escolher_colunas_cadastro(df)
    keys = {c: normalizar_descricao(c) for c in df.columns}
    centro_col = next((c for c,k in keys.items() if "CENTRO" in k and "CUSTO" in k), None)
    rows=[]
    for _,r in df.iterrows():
        code=str(r.get(cod_col,"") or "").strip()
        d1=str(r.get(desc_col,"") or "").strip()
        d2=str(r.get(desc2_col,"") or "").strip() if desc2_col else ""
        cc=str(r.get(centro_col,"") or "").strip() if centro_col else ""
        if len(normalizar_descricao(d2))<5: d2=""
        ncm=str(r.get("Pos.IPI/NCM","") or "")
        if not code or not d1: continue
        rows.append({"codigo":code,"descricao":d1,"descricao_extra":d2,"centro_custos":cc,"texto_match":normalizar_descricao(d1),"texto_match_extra":normalizar_descricao(d2),"ncm":re.sub(r"\D","",ncm)})
    return {"rows":rows}

def _tokens_informativos(texto: str) -> list[str]:
    stops = {
        "DE","DA","DO","DAS","DOS","PARA","COM","SEM","EM","NO","NA",
        "NOS","NAS","E","A","O","AS","OS","UM","UMA","UN","UND",
        "UNID","PC","PCES","PÇ","SV","SERVICO","SERVICOS","SERVIÇO","SERVIÇOS",
    }
    tokens = re.findall(r"[A-Z0-9]+", normalizar_texto(texto))
    return [t for t in tokens if t not in stops and len(t) >= 2]


def _score_descricao(a: str, b: str) -> float:
    na, nb = normalizar_descricao(a), normalizar_descricao(b)
    if not na or not nb:
        return 0.0
    ta, tb = _tokens_informativos(na), _tokens_informativos(nb)
    sa, sb = set(ta), set(tb)
    common = sa & sb
    coverage = len(common) / max(1, len(sa))
    precision = len(common) / max(1, len(sb))
    f1 = 2 * coverage * precision / max(0.001, coverage + precision)
    ratio = fuzz.ratio(na, nb)
    sort = fuzz.token_sort_ratio(na, nb)
    score = f1 * 48 + sort * 0.28 + ratio * 0.24
    if na == nb:
        return 100.0
    if len(ta) >= 3 and len(tb) >= 3 and (na in nb or nb in na):
        score = max(score, min(97.0, 72 + f1 * 25))
    numeric_a = set(re.findall(r"\d+(?:[.,]\d+)?", na))
    numeric_b = set(re.findall(r"\d+(?:[.,]\d+)?", nb))
    if numeric_a and numeric_b:
        num_cov = len(numeric_a & numeric_b) / len(numeric_a)
        score += min(12, num_cov * 12)
    return round(min(100.0, score), 1)

def _score_orcamento_descricao(a: str, b: str) -> float:
    """Pontuação específica para orçamento, com medidas muito pesadas."""
    na, nb = normalizar_descricao(a), normalizar_descricao(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0

    ta, tb = set(_tokens_informativos(na)), set(_tokens_informativos(nb))
    common = ta & tb
    coverage = len(common) / max(1, len(ta))
    precision = len(common) / max(1, len(tb))
    f1 = 2 * coverage * precision / max(0.001, coverage + precision)
    token_ratio = fuzz.token_sort_ratio(na, nb)

    nums_a = [parse_num(x) for x in re.findall(r"\d+(?:[.,]\d+)?", na)]
    nums_b = [parse_num(x) for x in re.findall(r"\d+(?:[.,]\d+)?", nb)]
    nums_a = [x for x in nums_a if x is not None]
    nums_b = [x for x in nums_b if x is not None]
    matched = 0
    used = set()
    for qa in nums_a:
        candidates = [(abs(qa-cb), j) for j,cb in enumerate(nums_b) if j not in used]
        if not candidates:
            continue
        diff,j=min(candidates)
        tol=max(0.0001, abs(qa)*0.002)
        if diff <= tol:
            matched += 1
            used.add(j)
    numeric_coverage = matched / max(1, len(nums_a))


    score = (f1 * 0.48) + (token_ratio * 0.22) + (numeric_coverage * 30.0)
    if nums_a and numeric_coverage < 0.5:
        score -= 12.0
    elif nums_a and numeric_coverage < 1.0:
        score -= 5.0
    return round(max(0.0, min(100.0, score)), 1)


def _candidatos_cadastro(desc: str, cadastro_rows: list[dict], ncm: str = "", service: bool = False):
    if not cadastro_rows:
        return []
    choices = []
    meta = []
    for global_i, r in enumerate(cadastro_rows):
        for field in ["texto_match", "texto_match_extra"]:
            if r.get(field):
                choices.append(r[field])
                meta.append(global_i)
    if not choices:
        return []
    q = normalizar_descricao(desc)


    exact_indices = [i for i, c in enumerate(choices) if c == q]
    if exact_indices:
        ranked = []
        seen = set()
        for i in exact_indices:
            gi = meta[i]
            if gi not in seen:
                ranked.append((100.0, gi)); seen.add(gi)
        return ranked

    candidate_indices = set()
    limits = 60 if not service else len(choices)
    for scorer in (fuzz.token_set_ratio, fuzz.token_sort_ratio, fuzz.WRatio, fuzz.ratio):
        for _, _, j in process.extract(q, choices, scorer=scorer, limit=min(limits, len(choices))):
            candidate_indices.add(meta[j])

    ranked = []
    for global_i in candidate_indices:
        r = cadastro_rows[global_i]
        scores = [_score_descricao(desc, r.get("descricao", ""))]
        if r.get("descricao_extra"):
            scores.append(_score_descricao(desc, r["descricao_extra"]))
        score = max(scores)

        qt = _tokens_informativos(desc)
        cand_tokens = set(_tokens_informativos(r.get("descricao", "")) + _tokens_informativos(r.get("descricao_extra", "")))
        common = set(qt) & cand_tokens
        if common:
            score += min(10.0, len(common) * 3.0)

        qnums = set(re.findall(r"\d+(?:[.,]\d+)?", normalizar_texto(desc)))
        cnums = set(re.findall(r"\d+(?:[.,]\d+)?", normalizar_texto(r.get("descricao", ""))))
        if qnums and cnums:
            score += min(8.0, (len(qnums & cnums) / len(qnums)) * 8.0)

        if (not service) and ncm and r.get("ncm") == ncm:
            score = min(100.0, score + 6.0)
        ranked.append((round(min(100.0, score), 1), global_i))
    return sorted(ranked, key=lambda x: (-x[0], x[1]))


def _formatar_outras_pecas(ranked, rows, best_index: int, best_score: float, forcar: bool = False) -> str:
    alternativas=[]
    if forcar:
        rel=max(12.0,min(25.0,best_score*0.30))
        for pos,(score,idx) in enumerate(ranked):
            if idx==best_index: continue
            if score>=max(30.0,best_score-rel) or pos<=2:
                codigo=str(rows[idx].get("codigo","")).strip()
                if codigo: alternativas.append(codigo)
            if len(alternativas)>=5: break
    else:
        rel=max(8.0,min(15.0,best_score*0.12))
        for score,idx in ranked:
            if idx==best_index or score<65.0: continue
            if score>=best_score-rel or score>=82.0:
                codigo=str(rows[idx].get("codigo","")).strip()
                if codigo: alternativas.append(codigo)
            if len(alternativas)>=5: break
    if not alternativas: return ""
    return "Outras peças viáveis: " + ", ".join(dict.fromkeys(alternativas))

def correlacionar(items: list[dict], cadastro: dict, centro_custo: str, tipo: str) -> list[dict]:
    rows = cadastro["rows"]
    out = []
    service = tipo == "NFS-E"

    for item in items:
        desc_exibicao = str(item.get("descricao_nota", "") or "").strip()
        desc_match = str(item.get("descricao_match") or desc_exibicao).strip()
        ocr = float(item.get("confianca_ocr") or 0)

        if service:


            ai_code = re.sub(r"\D", "", str(item.get("codigo_ia") or ""))[:12]
            best_row = next(
                (
                    row for row in rows
                    if re.sub(r"\D", "", str(row.get("codigo", "")))[:12] == ai_code
                ),
                None,
            )
            if best_row:
                code = str(best_row.get("codigo", ai_code)).strip()
                desc_db = best_row.get("descricao", "-")
                final = round(max(0.0, min(100.0, float(item.get("confianca_codigo_ia") or ocr or 85.0))), 1)
                reason = str(item.get("motivo_codigo_ia") or "").strip()
                reason = re.sub(r"(?i)\bGemini(?:-[A-Za-z0-9.-]+)?\b", "serviço de leitura", reason)
                reason = re.sub(r"(?i)\b(?:IA|intelig[êe]ncia artificial)\b", "leitura automática", reason)
                obs = f"Classificação automática: {reason}" if reason else "Classificação automática usando o catálogo de serviços."
            else:
                code = ""
                desc_db = "-"
                final = round(max(0.0, min(69.0, ocr)), 1)
                obs = (
                    f"REVISAR: a leitura retornou o código inexistente {ai_code}."
                    if ai_code
                    else "REVISAR: a leitura não definiu um código do catálogo de serviços."
                )

        else:
            ranked = _candidatos_cadastro(
                desc_match,
                rows,
                ncm=str(item.get("ncm", "") or ""),
                service=False,
            )
            best = ranked[0] if ranked else None
            best_score = best[0] if best else 0.0
            best_index = best[1] if best else -1
            best_row = rows[best_index] if best else None
            base_ocr = ocr if ocr > 0 else 96.0
            confidence = round(min(100.0, best_score * 0.82 + base_ocr * 0.18), 1) if best_row else 0.0


            accepted = bool(best_row and best_score >= 58.0)

            if accepted:
                code = best_row.get("codigo", "")
                desc_db = best_row.get("descricao", "-")
                final = confidence
                outras = _formatar_outras_pecas(ranked, rows, best_index, best_score, forcar=(final < 70))
                if outras:
                    obs = outras
                    if final < 70:
                        obs += " | Conferência recomendada devido à confiança da correlação."
                elif final < 70:
                    obs = "Conferência recomendada: confiança da correlação abaixo de 70%."
                else:
                    obs = ""
            else:
                code = ""
                desc_db = "-"
                final = confidence
                outras = _formatar_outras_pecas(ranked, rows, best_index, best_score, forcar=True) if best else ""
                if not best_row:
                    obs = "REVISAR: não foi possível correlacionar com o cadastro de produtos."
                elif outras:
                    obs = (
                        "REVISAR: nenhuma correspondência atingiu segurança suficiente. "
                        + outras
                    )
                else:
                    obs = "REVISAR: correspondência insuficiente para definir o código do produto com segurança."

        base_ocr = ocr if ocr > 0 else 96.0
        db_conf = round(final, 1)
        desc_conf = round(min(100.0, max(45.0, base_ocr + (5.0 if desc_exibicao else -15.0))), 1)
        qtd_conf = round(float(item.get("confianca_quantidade") or (base_ocr if item.get("quantidade") else 45.0)), 1)
        num_conf = round(float(item.get("confianca_numero") or (base_ocr if item.get("numero") not in {"", "SemNumero"} else 35.0)), 1)
        emp_conf = round(float(item.get("confianca_empresa") or (base_ocr if item.get("empresa") not in {"", "Empresa não identificada"} else 35.0)), 1)
        base = {
            "Descrição na Nota": desc_exibicao,
            "Quantidade": item.get("quantidade", "1"),
            "Código no Banco de Dados": code,
            "Descrição no Banco de Dados": desc_db,
            "Centro de Custos": centro_custo,
            "Confiança": f"{round(final)}%",
            "Observações": obs,
            "confiancas": {
                "Número da Nota": num_conf,
                "Empresa": emp_conf,
                "Descrição na Nota": desc_conf,
                "Quantidade": qtd_conf,
                "Código no Banco de Dados": db_conf,
                "Descrição no Banco de Dados": db_conf,
                "Centro de Custos": 100.0,
                "Observações": 100.0,
            },
        }
        if service:
            base["confiancas"]["Valor Total"] = round(float(item.get("confianca_valor_total") or base_ocr), 1)
            base = {"Número da Nota": item.get("numero", "SemNumero"), "Empresa": item.get("empresa", "Empresa não identificada"), **base, "Valor Total": item.get("valor_total", "-"), "Arquivo": item.get("arquivo", "")}
        else:
            codigo_emitente = str(item.get("codigo_produto_emitente") or "").strip()
            base["confiancas"]["Código do Produto do Emitente"] = round(
                float(item.get("confianca_codigo_produto") or (base_ocr if codigo_emitente else 35.0)),
                1,
            )
            base = {
                "Número da Nota": item.get("numero", "SemNumero"),
                "Empresa": item.get("empresa", "Empresa não identificada"),
                "Código do Produto do Emitente": codigo_emitente,
                **base,
                "Arquivo": item.get("arquivo", ""),
            }

        try:
            if service:
                ordered_rows = ([best_row] if best_row else []) + [row for row in rows if row is not best_row]
                base["_candidatos"] = [
                    {
                        "codigo": str(row.get("codigo", "")),
                        "descricao": str(row.get("descricao", "")),
                        "score": round(float(final), 1) if row is best_row else 0.0,
                    }
                    for row in ordered_rows[:6]
                ]
            else:
                cand_rank = _candidatos_cadastro(desc_match, rows, ncm=str(item.get("ncm", "") or ""), service=False)
                base["_candidatos"] = [
                    {"codigo": str(rows[idx].get("codigo", "")), "descricao": str(rows[idx].get("descricao", "")), "score": round(float(score), 1)}
                    for score, idx in cand_rank[:6]
                ]
        except Exception:
            base["_candidatos"] = []
        base["_id"] = f"{tipo.lower()}-{uuid.uuid4().hex[:10]}"
        base["_document_id"] = item.get("_document_id", "")
        base["_tipo"] = tipo
        out.append(base)

    return out


def _orcamento_linha_produto_re(line: str):
    """Reconhece a linha tabular real de um orçamento.

    Formato típico do orçamento de referência:
    CODIGO DESCRICAO NCM UN QTDE_PECAS QTDE UNITARIO IPI ICMS ST TOTAL

    O primeiro campo é o código do fornecedor; a quantidade efetiva é validada
    pela conta QTDE × UNITÁRIO ≈ TOTAL, evitando confundir QTDE PEÇAS com a
    quantidade faturada, algo que acontece em itens vendidos por metro.
    """
    raw = re.sub(r"\s+", " ", str(line or "")).strip()
    m = re.match(
        r"^(?P<codigo>[A-Z0-9][A-Z0-9._/-]{3,24})\s+"
        r"(?P<descricao>.+?)\s+"
        r"(?P<ncm>\d{8})\s+"
        r"(?P<un>[A-ZÇ0-9²³]+)\s+"
        r"(?P<nums>.+)$",
        raw,
        re.I,
    )
    if not m:
        return None
    nums = re.findall(r"(?<![\d/])(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{2,4})(?!\d)", m.group("nums"))
    if len(nums) < 5:
        return None
    values = [parse_num(x) for x in nums]
    if any(v is None for v in values):
        return None


    best = None
    for qty_idx in range(0, min(3, len(values)-2)):
        for unit_idx in range(qty_idx + 1, min(qty_idx + 4, len(values)-1)):
            for total_idx in range(unit_idx + 1, len(values)):
                qty, unit, total = values[qty_idx], values[unit_idx], values[total_idx]
                if qty is None or unit is None or total is None or qty <= 0 or unit < 0:
                    continue
                calc = qty * unit
                diff = abs(calc - total)
                tol = max(0.05, abs(total) * 0.003)
                if diff <= tol:
                    distance_penalty = (qty_idx * 3) + max(0, unit_idx - 2)
                    score = diff * 100 + distance_penalty
                    if best is None or score < best[0]:
                        best = (score, qty, unit, total, qty_idx, unit_idx, total_idx, values)
    if best is None:
        return None
    _, qty, unit, total, qty_idx, unit_idx, total_idx, all_values = best
    return {
        "codigo_orcamento": m.group("codigo"),
        "descricao": m.group("descricao").strip(),
        "ncm": m.group("ncm"),
        "unidade": m.group("un"),
        "quantidade": qty,
        "preco_unitario": unit,
        "total_peca": total,
        "valores": all_values,
        "qtde_pecas": all_values[0] if all_values else None,
    }


def _orcamento_total_por_rotulo(texto: str) -> float | None:
    m = re.search(r"TOTAL\s+DO\s+PEDIDO\s+((?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})", normalizar_texto(texto), re.I)
    return parse_num(m.group(1)) if m else None


def _candidatos_orcamento_rapido(desc: str, cadastro_rows: list[dict], limit: int = 18) -> list[tuple[float,int]]:
    if not cadastro_rows:
        return []
    q_norm = normalizar_descricao(desc)
    q_tokens = set(_tokens_informativos(desc))
    q_nums = set(re.findall(r"\d+(?:[.,]\d+)?", normalizar_texto(desc)))


    candidate_indices = set()
    token_counts = []
    for i, r in enumerate(cadastro_rows):
        ctext = normalizar_descricao(r.get("descricao", ""))
        if not ctext:
            continue
        ctokens = set(_tokens_informativos(ctext))
        common = len(q_tokens & ctokens)
        if common:
            token_counts.append((common, i))
    token_counts.sort(key=lambda x: (-x[0], x[1]))
    candidate_indices.update(i for _, i in token_counts[:120])

    choices = []
    meta = []
    for i, r in enumerate(cadastro_rows):
        for field in ("texto_match", "texto_match_extra"):
            if r.get(field):
                choices.append(r[field]); meta.append(i)
    if choices:
        for scorer in (fuzz.token_set_ratio, fuzz.token_sort_ratio, fuzz.WRatio):
            for _, _, local_idx in process.extract(q_norm, choices, scorer=scorer, limit=min(limit * 4, len(choices))):
                candidate_indices.add(meta[local_idx])

    ranked=[]
    for gi in candidate_indices:
        r=cadastro_rows[gi]
        score=max(_score_orcamento_descricao(desc, r.get("descricao", "")), _score_orcamento_descricao(desc, r.get("descricao_extra", "")) if r.get("descricao_extra") else 0)
        ctokens=set(_tokens_informativos(r.get("descricao", "")))
        common=q_tokens & ctokens
        score += min(10.0, len(common) * 2.5)
        c_nums=set(re.findall(r"\d+(?:[.,]\d+)?", normalizar_texto(r.get("descricao", ""))))
        if q_nums:
            overlap = len(q_nums & c_nums) / len(q_nums)
            score += min(6.0, 6.0 * overlap)
        ranked.append((round(min(100.0, max(0.0, score)),1), gi))
    return sorted(ranked,key=lambda x:(-x[0],x[1]))


def _deep_budget_extract_native(pdf_bytes: bytes) -> tuple[list[dict], float | None]:
    """Primeira leitura do Deep Reading usando o texto nativo do PDF.

    Quando o orçamento é um PDF digital, o texto nativo preserva a tabela muito
    melhor que OCR puro. O OCR entra depois como validação, não como substituto.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full = "\n".join(page.extract_text(x_tolerance=2, y_tolerance=3) or "" for page in pdf.pages)
    items=[]
    for line in full.splitlines():
        parsed=_orcamento_linha_produto_re(line)
        if parsed:
            items.append(parsed)
    total=_orcamento_total_por_rotulo(full)
    return items,total


def _extrair_itens_orcamento_de_estruturas(parsed_items: list[dict], cadastro: dict, total_pedido: float | None, source_conf: float = 95.0) -> list[dict]:
    resultados=[]
    for item in parsed_items:
        desc=item["descricao"]
        ranked=_candidatos_orcamento_rapido(desc,cadastro["rows"],limit=20)


        ncm_item=re.sub(r"\D","",str(item.get("ncm","") or ""))
        adjusted=[]
        for score,idx in ranked:
            cand_ncm=re.sub(r"\D","",str(cadastro["rows"][idx].get("ncm","") or ""))
            sscore=float(score)
            if ncm_item and cand_ncm:
                if cand_ncm==ncm_item: sscore=min(100.0,sscore+6.0)
                else: sscore-=4.0
            adjusted.append((round(max(0.0,min(100.0,sscore)),1),idx))
        ranked=sorted(adjusted,key=lambda x:(-x[0],x[1]))
        best_score,best_idx=ranked[0] if ranked else (0.0,-1)
        best_row=cadastro["rows"][best_idx] if best_idx>=0 else None
        second=ranked[1][0] if len(ranked)>1 else 0.0

        confiavel=bool(best_row and best_score>=94.0 and (best_score-second>=6.0 or best_score>=98.0))
        desc_conf=min(99.0, max(80.0, float(source_conf)))
        numeric_conf=98.0 if abs(item["quantidade"]*item["preco_unitario"]-item["total_peca"]) <= max(0.05,item["total_peca"]*0.003) else 78.0
        candidatos=[
            {"codigo":str(cadastro["rows"][idx].get("codigo","")),"descricao":str(cadastro["rows"][idx].get("descricao","")),"score":round(float(score),1)}
            for score,idx in ranked[:8]
        ]


        resultados.append({
            "Nome no Orçamento": item["codigo_orcamento"],
            "Código no Banco de Dados": str(best_row.get("codigo","")) if confiavel else "",
            "Descrição no Orçamento": desc,
            "Descrição no Banco de Dados": str(best_row.get("descricao","")) if confiavel else "",
            "Quantidade": format_num(item["quantidade"],3),
            "Preço Unitário": format_num(item["preco_unitario"],2),
            "Total da Peça": format_num(item["total_peca"],2),
            "Total da Peça Num": item["total_peca"],
            "Total do Pedido": format_num(total_pedido,2) if total_pedido is not None else "-",
            "_score":round(float(best_score),1),
            "_candidatos":candidatos,
            "_revisar":not confiavel,
            "_confiancas":{
                "Nome no Orçamento":desc_conf,
                "Código no Banco de Dados":round(best_score if best_row else 35.0,1),
                "Descrição no Orçamento":desc_conf,
                "Descrição no Banco de Dados":round(best_score if best_row else 35.0,1),
                "Quantidade":numeric_conf,
                "Preço Unitário":numeric_conf,
                "Total da Peça":numeric_conf,
                "Total do Pedido":98.0 if total_pedido is not None else 40.0,
            },
        })
    return resultados


def deep_read_budget(pdf_bytes: bytes, filename: str, cadastro: dict) -> list[dict]:
    if not HAS_OCR:
        raise RuntimeError("OCR indisponível. Configure o Tesseract antes de usar o Deep Reading.")


    parsed_items,total_native=_deep_budget_extract_native(pdf_bytes)


    source_conf=96.0 if parsed_items else 70.0
    if not parsed_items or total_native is None:
        doc=pdfium.PdfDocument(pdf_bytes)
        ocr_lines=[]
        for page_idx in range(len(doc)):
            image=doc[page_idx].render(scale=350/72).to_pil()
            r=ocr_extract(image,detalhar=True,timeout=float(os.getenv("NFREADER_DEEP_OCR_TIMEOUT", "45")))
            ocr_lines.extend(r.text.splitlines())
        fallback=[]
        for line in ocr_lines:
            parsed=_orcamento_linha_produto_re(line)
            if parsed:
                fallback.append(parsed)
        if fallback:
            parsed_items=fallback
        if total_native is None:
            total_native=_orcamento_total_por_rotulo("\n".join(ocr_lines))
        source_conf=92.0 if parsed_items else 60.0

    rows=_extrair_itens_orcamento_de_estruturas(parsed_items,cadastro,total_native,source_conf)
    for i,row in enumerate(rows,1):
        row["_id"]=f"orc-{i}-{uuid.uuid4().hex[:8]}"
        row["_tipo"]="ORCAMENTO"
        row["Arquivo"]=filename
    return rows


def motivo_outro(tipo: str, page: dict) -> str:
    if page.get("error"): return page["error"]
    if tipo == "BOLETO": return "Boleto detectado; documentos bancários não são processados pelo sistema."
    if tipo == "CTE": return "CT-e/DACTE detectado; somente NF-e e NFS-e são processadas."
    if page.get("ocr") and page.get("conf", 0) < 50: return "Documento não legível o suficiente após OCR (baixa qualidade da imagem)."
    if len(page.get("text", "").strip()) < 30: return "Texto insuficiente para identificar o documento."
    return "Tipo de documento não identificado como NF-e ou NFS-e."


def style_workbook(writer) -> None:
    blue = PatternFill("solid", fgColor="D9EAF7")
    yellow = PatternFill("solid", fgColor="FFF2CC")
    orange = PatternFill("solid", fgColor="FCE4D6")
    red = PatternFill("solid", fgColor="F4CCCC")
    for ws in writer.book.worksheets:
        ws.freeze_panes = "A2"
        if ws.max_row > 1:
            ws.auto_filter.ref = ws.dimensions
        if ws.title == "Produtos (NF-e)":
            quantity_column = next((cell.column for cell in ws[1] if cell.value == "Quantidade"), None)
            if quantity_column is not None:
                for row_number in range(2, ws.max_row + 1):
                    cell = ws.cell(row_number, quantity_column)
                    cell.value = "" if cell.value is None else str(cell.value)
                    cell.number_format = "@"
        for c in ws[1]:
            c.font = Font(bold=True)
            c.fill = blue
            c.alignment = Alignment(horizontal="center", vertical="center")
        for row in ws.iter_rows(min_row=2):
            conf = 100
            for c in row:
                if str(ws.cell(1,c.column).value or "") == "Confiança":
                    m = re.search(r"\d+", str(c.value or ""))
                    conf = int(m.group()) if m else 0
                    break
            fill = red if conf < 50 else orange if conf < 70 else yellow if conf <= 80 else None
            if fill:
                for c in row: c.fill = fill
        for col in range(1, ws.max_column + 1):
            samples = [len(str(ws.cell(r,col).value or "")) for r in range(1, min(ws.max_row, 100)+1)]
            ws.column_dimensions[get_column_letter(col)].width = min(55, max(12, max(samples, default=12)+2))
            for r in range(1, ws.max_row+1):
                ws.cell(r,col).alignment = Alignment(vertical="top", wrap_text=True)


@lru_cache(maxsize=1)
def load_fixed_cadastros() -> tuple[dict, dict]:
    return cadastro_preparado(str(PRODUTOS_XLSX)), cadastro_preparado(str(SERVICOS_XLSX))

@app.get("/cadastro/codigo")
async def consultar_codigo_cadastro(tipo: str = "produtos", codigo: str = ""):
    from fastapi import HTTPException
    termo = str(codigo or "").strip()
    if not termo:
        raise HTTPException(status_code=400, detail="Informe o código do cadastro.")

    produtos, servicos = load_fixed_cadastros()
    cadastro = produtos if normalizar_texto(tipo) in {"PRODUTOS", "PRODUTO", "NFE", "NF-E"} else servicos

    def variantes(valor: str) -> set[str]:
        bruto = str(valor or "").strip().upper()
        compacto = re.sub(r"[^A-Z0-9]", "", bruto)
        return {v for v in (bruto, compacto) if v}

    alvo = variantes(termo)
    for row in cadastro["rows"]:
        if alvo & variantes(str(row.get("codigo", ""))):
            return {
                "encontrado": True,
                "resultado": {
                    "codigo": str(row.get("codigo", "")),
                    "descricao": str(row.get("descricao", "")),
                    "centro_custos": str(row.get("centro_custos", "")),
                    "ncm": str(row.get("ncm", "")),
                    "unidade": str(row.get("unidade", "") or ""),
                },
            }

    return {"encontrado": False, "resultado": None}


@app.get("/cadastro/pesquisar")
async def pesquisar_cadastro(tipo: str = "produtos", q: str = "", limite: int = 50):
    from fastapi import HTTPException
    termo=normalizar_descricao(q)
    if len(termo)<2: raise HTTPException(status_code=400,detail="Informe pelo menos 2 caracteres para pesquisar.")
    produtos,servicos=load_fixed_cadastros()
    cadastro=produtos if normalizar_texto(tipo) in {"PRODUTOS","PRODUTO","NFE","NF-E"} else servicos
    choices=[]; meta=[]
    for i,r in enumerate(cadastro["rows"]):
        for field in ("texto_match","texto_match_extra"):
            if r.get(field): choices.append(r[field]); meta.append(i)
    hits=[]
    for scorer in (fuzz.WRatio,fuzz.token_set_ratio,fuzz.token_sort_ratio):
        hits.extend(process.extract(termo,choices,scorer=scorer,limit=min(max(10,limite),len(choices))))
    best={}
    for _,score,local_idx in hits:
        gi=meta[local_idx]; row=cadastro["rows"][gi]
        detalhado=max(_score_descricao(q,row.get("descricao","")),_score_descricao(q,row.get("descricao_extra","")) if row.get("descricao_extra") else 0)
        best[gi]=max(best.get(gi,0.0),float(score),float(detalhado))
    saida=[]
    for gi,score in sorted(best.items(),key=lambda kv:(-kv[1],kv[0]))[:max(1,min(100,limite))]:
        row=cadastro["rows"][gi]
        saida.append({"codigo":str(row.get("codigo","")),"descricao":str(row.get("descricao","")),"centro_custos":str(row.get("centro_custos","")),"ncm":str(row.get("ncm","")),"score":round(float(score),1)})
    return {"tipo":tipo,"busca":q,"resultados":saida}


def _prepare_uploaded_files(nome: str, data: bytes) -> tuple[list[tuple[str, bytes]], list[dict]]:
    arquivos: list[tuple[str, bytes]] = []
    outros_rows: list[dict] = []
    if nome.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for entry in z.infolist():
                    if entry.is_dir():
                        continue
                    entry_name = Path(entry.filename).name
                    if entry_name.lower().endswith(".pdf"):
                        arquivos.append((entry_name, z.read(entry.filename)))
                    else:
                        outros_rows.append({"Arquivo":entry_name,"Tipo Detectado":"Arquivo não suportado","Motivo":"O ZIP contém um arquivo que não é PDF.","Confiança":"100%","Observações":"Movido para Outros automaticamente."})
        except zipfile.BadZipFile:
            raise RuntimeError("O arquivo enviado não é um ZIP válido.")
    elif nome.lower().endswith(".pdf"):
        arquivos=[(nome,data)]
    else:
        raise RuntimeError("Envie um arquivo PDF ou ZIP contendo PDFs.")
    return arquivos, outros_rows


def _build_review_payload(session_id: str, session: dict) -> dict:
    documentos=[]
    for doc_id, doc in session["documents"].items():
        pages=doc.get("pages",[])
        range_label = str(pages[0]) if len(pages)==1 else f"{pages[0]}–{pages[-1]}" if pages else ""
        documentos.append({
            "id":doc_id,
            "arquivo":doc["filename"],
            "url":f"/documento/{session_id}/{doc_id}",
            "paginas":pages,
            "tipo":doc.get("tipo",""),
            "origem":doc.get("source_filename",doc["filename"]),
            "intervalo":range_label,
        })
    def review_rows(rows:list[dict])->list[dict]:
        out=[]
        for row in rows:
            clean=dict(row)
            clean["candidatos"]=clean.pop("_candidatos",[])
            clean["confiancas"]=clean.pop("_confiancas",{})
            clean["id"] = clean.pop("_id",uuid.uuid4().hex)
            clean["document_id"]=clean.pop("_document_id","")
            clean.pop("_tipo",None)
            clean.pop("_score",None)
            out.append(clean)
        return out
    return {"session_id":session_id,"modo":session["modo"],"documentos":documentos,"resumo":{"nfe":len(session["nfe_rows"]),"nfse":len(session["nfse_rows"]),"cte":len(session["cte_rows"]),"outros":len(session["outros_rows"]),"orcamento":len(session["orcamento_rows"])},"nfe_rows":review_rows(session["nfe_rows"]),"nfse_rows":review_rows(session["nfse_rows"]),"cte_rows":review_rows(session["cte_rows"]),"outros_rows":_sanitizar_review_rows(session["outros_rows"]),"orcamento_rows":review_rows(session["orcamento_rows"])}


def _process_normal_with_gemini(
    arquivos: list[tuple[str, bytes]],
    produtos: dict,
    servicos: dict,
    centro_custo: str,
    session_id: str,
    session: dict,
    job_id: str | None,
) -> dict:
    """Processa notas pelo motor visual mantendo o contrato atual do frontend."""
    total_files = max(1, len(arquivos))
    if job_id:
        ANALYSIS_JOBS[job_id].update({
            "status": "processing", "progress": 0, "completed": 0,
            "total": sum(_pdf_page_count(data) for _, data in arquivos),
            "current": "Preparando fila de documentos...", "session_id": session_id,
        })

    try:
        def queue_progress(done: int, total: int, message: str = "") -> None:
            if not job_id:
                return
            ANALYSIS_JOBS[job_id].update({
                "progress": min(90, int(done / max(1, total) * 90)),
                "completed": done,
                "total": total,
                "current": message or f"Leitura em andamento: {done} de {total} páginas concluídas.",
            })

        analyzed_files = analyze_fiscal_pdfs(
            arquivos, queue_progress, servicos.get("rows", [])
        )
    except GeminiError:
        raise
    except Exception as exc:
        raise GeminiError(f"Falha inesperada durante a leitura dos documentos: {exc}") from exc

    for file_index, (source_name, pdf_bytes, documents) in enumerate(analyzed_files, start=1):

        for doc_index, document in enumerate(documents, start=1):
            pages = document.get("pages", [])
            if not pages:
                continue
            range_label = str(pages[0]) if len(pages) == 1 else f"{pages[0]}–{pages[-1]}"
            doc_name = f"{source_name} (página {range_label})"
            doc_type = str(document.get("document_type", "OUTRO"))
            confidence = float(document.get("confidence", 0) or 0)
            doc_id = uuid.uuid4().hex
            try:
                subset = _subset_pdf(pdf_bytes, pages)
            except Exception:
                subset = pdf_bytes
            session["documents"][doc_id] = {
                "filename": doc_name,
                "bytes": subset,
                "source_filename": source_name,
                "pages": pages,
                "tipo": doc_type,
            }

            number = str(document.get("number") or "SemNumero")
            provider = str(document.get("provider_name") or "Empresa não identificada")
            base_conf = max(0.0, min(100.0, confidence))

            if doc_type == "NFSE":
                description = str(document.get("service_description") or "Prestação de serviço")
                ai_service_code = str(document.get("service_code") or "").strip()
                ai_service_reason = str(document.get("service_code_reason") or "").strip()
                if "ANDAIMES ELIMAR" in normalizar_texto(provider):
                    ai_service_code = "715237"
                    ai_service_reason = "Regra fixa: notas da ANDAIMES ELIMAR usam Locação de Andaimes."
                item = {
                    "numero": number,
                    "empresa": provider,
                    "descricao_nota": description,
                    "descricao_match": description,
                    "quantidade": 1.0,
                    "valor_total": str(document.get("total_value") or "-"),
                    "confianca_ocr": base_conf,
                    "confianca_numero": base_conf if number != "SemNumero" else 35.0,
                    "confianca_empresa": base_conf if provider != "Empresa não identificada" else 35.0,
                    "confianca_quantidade": 100.0,
                    "confianca_valor_total": base_conf if document.get("total_value") not in {"", "-", None} else 35.0,
                    "codigo_ia": ai_service_code,
                    "motivo_codigo_ia": ai_service_reason,
                    "confianca_codigo_ia": base_conf,
                    "arquivo": doc_name,
                    "_document_id": doc_id,
                }
                session["nfse_rows"].extend(correlacionar([item], servicos, centro_custo, "NFS-E"))
                continue

            if doc_type == "NFE":
                extracted_items = document.get("items", [])
                if not extracted_items:
                    session["outros_rows"].append({
                        "Arquivo": doc_name,
                        "Tipo Detectado": "NF-e",
                        "Motivo": "A leitura identificou uma NF-e, mas não encontrou produtos legíveis.",
                        "Confiança": f"{round(base_conf)}%",
                        "Observações": str(document.get("notes") or "Revisar o documento."),
                    })
                    continue
                payload = [{
                    "numero": number,
                    "empresa": provider,
                    "codigo_produto_emitente": str(item.get("product_code") or ""),
                    "descricao_nota": str(item.get("description") or ""),
                    "quantidade": item.get("quantity") or "",
                    "ncm": str(item.get("ncm") or ""),
                    "confianca_ocr": base_conf,
                    "confianca_quantidade": base_conf,
                    "confianca_numero": base_conf if number != "SemNumero" else 35.0,
                    "confianca_empresa": base_conf if provider != "Empresa não identificada" else 35.0,
                    "arquivo": doc_name,
                    "_document_id": doc_id,
                } for item in extracted_items]
                session["nfe_rows"].extend(correlacionar(payload, produtos, centro_custo, "NF-E"))
                continue

            if doc_type == "CTE":
                synthetic_lines = [f"CT-e Nº {number}", "EMITENTE", provider]
                session["cte_rows"].append(build_cte_row(
                    synthetic_lines, servicos, centro_custo, doc_name, doc_id, base_conf
                ))
                continue

            type_label = "Boleto" if doc_type == "BOLETO" else "Desconhecido"
            default_reason = (
                "Boleto detectado; documentos bancários não são processados pelo sistema."
                if doc_type == "BOLETO"
                else "Documento não identificado como NF-e ou NFS-e durante a leitura."
            )
            session["outros_rows"].append({
                "Arquivo": doc_name,
                "Tipo Detectado": type_label,
                "Motivo": default_reason,
                "Confiança": f"{round(base_conf)}%",
                "Observações": str(document.get("notes") or ""),
            })

        if job_id:
            ANALYSIS_JOBS[job_id].update({
                "progress": 90 + int(file_index / total_files * 10),
                "completed": file_index,
                "total": total_files,
                "current": f"Organizando arquivo {file_index} de {total_files}",
            })

    REVISION_SESSIONS[session_id] = session
    result = _build_review_payload(session_id, session)
    if job_id:
        ANALYSIS_JOBS[job_id].update({
            "status": "done", "progress": 100, "current": "Análise concluída.", "result": result
        })
    return result


def _pdf_page_count(pdf_bytes: bytes) -> int:
    try:
        return len(pdfium.PdfDocument(pdf_bytes))
    except Exception:
        return 1


def _process_analysis(centroCusto: str, modo: str, nome: str, data: bytes, job_id: str | None = None, owner: str | None = None) -> dict:
    _limpar_sessoes_expiradas()
    try:
        produtos, servicos = load_fixed_cadastros()
    except Exception as e:
        raise RuntimeError(f"Falha ao carregar cadastros fixos: {e}")

    arquivos, outros_rows = _prepare_uploaded_files(nome, data)
    session_id=uuid.uuid4().hex
    session={"created_at":time.time(),"owner":owner,"modo":modo,"centro_custo":centroCusto,"documents":{},"nfe_rows":[],"nfse_rows":[],"cte_rows":[],"outros_rows":outros_rows,"orcamento_rows":[]}
    if modo != "deep":
        return _process_normal_with_gemini(
            arquivos, produtos, servicos, centroCusto, session_id, session, job_id
        )
    if job_id:
        ANALYSIS_JOBS[job_id].update({"status":"processing","progress":0,"total":sum(_pdf_page_count(b) for _,b in arquivos) or len(arquivos) or 1,"completed":0,"current":"Identificando páginas e documentos...","session_id":session_id})

    total_pages=max(1,sum(_pdf_page_count(b) for _,b in arquivos))
    segmented_documents: list[dict]=[]
    segmented_done=0

    def mark_segmentation(done:int,total:int,current:str):
        if job_id:
            overall=segmented_done + done
            pct=int(round((overall/max(1,total_pages))*30))
            ANALYSIS_JOBS[job_id].update({"progress":max(0,min(30,pct)),"completed":overall,"current":current})

    for nome_arq,pdf_bytes in arquivos:
        try:
            segments=segment_pdf(pdf_bytes,nome_arq,progress_callback=mark_segmentation)
        except Exception as e:


            count=_pdf_page_count(pdf_bytes)
            tipo_fallback="OUTRO"
            segments=[{
                "index":1,"pages":list(range(1,count+1)),"tipo":tipo_fallback,"confianca":25.0,
                "arquivo":f"{nome_arq} (páginas 1–{count})" if count>1 else nome_arq,
                "bytes":pdf_bytes,"signatures":[],
            }]
            if job_id:
                ANALYSIS_JOBS[job_id]["current"] = f"Não foi possível segmentar {nome_arq}; tratando como documento único."
        segmented_documents.extend(segments)
        segmented_done += _pdf_page_count(pdf_bytes)

    total_groups=len(segmented_documents)
    if total_groups==0:
        REVISION_SESSIONS[session_id]=session
        if job_id:
            ANALYSIS_JOBS[job_id].update({"status":"done","progress":100,"current":"Nenhum documento encontrado.","result":_build_review_payload(session_id,session)})
        return _build_review_payload(session_id,session)

    def mark_processing(done:int,current:str):
        if job_id:
            pct=30 + int(round(done/max(1,total_groups)*70))
            ANALYSIS_JOBS[job_id].update({"progress":max(30,min(100,pct)),"completed":done,"current":current})

    for idx,group in enumerate(segmented_documents,start=1):
        nome_doc=group["arquivo"]
        pdf_doc=group["bytes"]
        pages_numbers=group.get("pages",[])
        tipo=group.get("tipo","OUTRO")
        doc_id=uuid.uuid4().hex
        session["documents"][doc_id]={
            "filename":nome_doc,
            "bytes":pdf_doc,
            "source_filename":group.get("source_filename") or nome_doc.split(" (página")[0].split(" (páginas")[0],
            "pages":pages_numbers,
            "tipo":tipo,
        }

        if modo == "deep":
            try:
                if job_id:
                    ANALYSIS_JOBS[job_id]["current"]=f"Deep Reading {idx} de {total_groups}: {nome_doc}"
                rows=deep_read_budget(pdf_doc,nome_doc,produtos)
            except Exception as e:
                rows=[]
                session["outros_rows"].append({"Arquivo":nome_doc,"Tipo Detectado":"Orçamento","Motivo":"Falha no Deep Reading.","Confiança":"0%","Observações":str(e)})
            if rows:
                for row in rows:
                    row["_document_id"]=doc_id
                    row["_tipo"]="ORCAMENTO"
                session["orcamento_rows"].extend(rows)
            elif not any(r.get("Arquivo")==nome_doc for r in session["outros_rows"]):
                session["outros_rows"].append({"Arquivo":nome_doc,"Tipo Detectado":"Não identificado","Motivo":"O Deep Reading não encontrou uma tabela de itens de orçamento reconhecível.","Confiança":"0%","Observações":"Revisar o documento ou usar o modo normal para classificação."})
            mark_processing(idx,f"Documento {idx} de {total_groups} concluído")
            continue

        try:
            pages=process_pdf_pages(pdf_doc)
        except Exception as e:
            session["outros_rows"].append({"Arquivo":nome_doc,"Tipo Detectado":"Falha","Motivo":"Falha no sistema ao processar o documento segmentado.","Confiança":"0%","Observações":str(e)})
            mark_processing(idx,f"Documento {idx} de {total_groups} concluído")
            continue

        combined_text="\n".join(p.get("text","") for p in pages)
        combined_norm=normalizar_texto(combined_text)


        if tipo=="NFSE":
            nfse_rows_pos = pages[0].get("rows",[]) if len(pages)==1 else []
            d=extract_nfse(combined_text.splitlines(), nfse_rows_pos)
            item={
                "numero":d["numero"],"empresa":d["empresa"],"descricao_nota":d["descricao"],
                "descricao_match":d.get("descricao_match",d["descricao"]),"quantidade":d["quantidade"],"valor_total":d["valor_total"],
                "confianca_ocr":max([float(p.get("conf",0) or 0) for p in pages] or [0]),
                "confianca_numero":95.0 if d.get("numero") not in {"","SemNumero"} else 35.0,
                "confianca_empresa":90.0 if d.get("empresa") not in {"","Empresa não identificada"} else 35.0,
                "confianca_quantidade":90.0,"confianca_valor_total":90.0,"arquivo":nome_doc,"_document_id":doc_id,
            }
            session["nfse_rows"].extend(correlacionar([item],servicos,centroCusto,"NFS-E"))
            mark_processing(idx,f"NFS-e {idx} de {total_groups} concluída")
            continue

        if tipo=="NFE":
            first_image=pages[0].get("image") if pages else None
            empresa=extract_nfe_emitter([line for p in pages for line in p.get("lines",[])], first_image)
            numero=extract_nfe_number([line for p in pages for line in p.get("lines",[])])
            items=[]
            for p in pages:
                items.extend(extract_nfe_items(p))
            if not items and pages:


                items=extract_nfe_native(combined_text.splitlines())
            if not items:
                session["outros_rows"].append({"Arquivo":nome_doc,"Tipo Detectado":"NF-e","Motivo":"NF-e identificada, mas nenhum item pôde ser extraído da tabela de produtos.","Confiança":f"{round(max([p.get('conf',0) for p in pages] or [0]))}%","Observações":"Provável baixa qualidade do OCR ou layout ainda não reconhecido; o documento permanece agrupado corretamente para revisão."})
                mark_processing(idx,f"NF-e {idx} de {total_groups} concluída")
                continue
            payload=[{
                "numero":numero,
                "empresa":empresa or "Empresa não identificada",
                "codigo_produto_emitente":str(x.get("codigo_fornecedor") or x.get("product_code") or ""),
                "descricao_nota":x["descricao"],
                "quantidade":x["quantidade"],
                "ncm":x.get("ncm",""),
                "confianca_ocr":max([float(p.get("conf",0) or 0) for p in pages] or [0]),
                "confianca_quantidade":x.get("confianca_quantidade",max([float(p.get("conf",0) or 0) for p in pages] or [0])),
                "confianca_numero":90.0 if numero not in {"","SemNumero"} else 35.0,
                "confianca_empresa":90.0 if empresa else 35.0,
                "arquivo":nome_doc,"_document_id":doc_id,
            } for x in items]
            session["nfe_rows"].extend(correlacionar(payload,produtos,centroCusto,"NF-E"))
            mark_processing(idx,f"NF-e {idx} de {total_groups} concluída")
            continue

        if tipo=="CTE":
            confidence=max([float(p.get("conf",0) or 0) for p in pages] or [float(group.get("confianca",0) or 0)])
            session["cte_rows"].append(build_cte_row(
                combined_text.splitlines(), servicos, centroCusto, nome_doc, doc_id, confidence
            ))
            mark_processing(idx,f"CT-e {idx} de {total_groups} concluído")
            continue


        if tipo == "BOLETO":
            tipo_label="Boleto"
            motivo=motivo_outro(tipo,{"text":combined_text,"ocr":True,"conf":max([p.get("conf",0) for p in pages] or [0]),"error":""})
        else:
            tipo_label="Desconhecido"
            motivo="Tipo de documento não identificado como NF-e ou NFS-e."
        conf=max([float(p.get("conf",0) or 0) for p in pages] or [float(group.get("confianca",0) or 0)])
        session["outros_rows"].append({"Arquivo":nome_doc,"Tipo Detectado":tipo_label,"Motivo":motivo,"Confiança":f"{round(conf)}%","Observações":"Documento agrupado por páginas antes da classificação final."})
        mark_processing(idx,f"Outro documento {idx} de {total_groups} concluído")

    REVISION_SESSIONS[session_id]=session
    return _build_review_payload(session_id, session)


@app.post("/analisar")
async def analisar_notas(request: Request, centroCusto: str = Form(""), modo: str = Form("normal"), notas: UploadFile = File(...)):
    data=await notas.read()
    nome=notas.filename or "documento.pdf"
    return await asyncio.to_thread(_process_analysis, centroCusto, modo, nome, data, None, request.state.username)


@app.post("/iniciar-analise")
async def iniciar_analise(request: Request, centroCusto: str = Form(""), modo: str = Form("normal"), notas: UploadFile = File(...)):
    data=await notas.read()
    nome=notas.filename or "documento.pdf"

    arquivos,_=_prepare_uploaded_files(nome,data)
    job_id=uuid.uuid4().hex
    owner=request.state.username
    ANALYSIS_JOBS[job_id]={"owner":owner,"status":"queued","progress":0,"completed":0,"total":len(arquivos),"current":"Preparando documentos...","result":None,"error":""}

    async def runner():
        try:
            result=await asyncio.to_thread(_process_analysis, centroCusto, modo, nome, data, job_id, owner)
            ANALYSIS_JOBS[job_id].update({"status":"done","progress":100,"current":"Leitura concluída.","result":result})
        except Exception as exc:
            ANALYSIS_JOBS[job_id].update({"status":"error","current":"Falha no processamento.","error":str(exc)})

    asyncio.create_task(runner())
    return {"job_id":job_id,"total":len(arquivos)}


@app.get("/progresso/{job_id}")
async def progresso(job_id: str, request: Request):
    from fastapi import HTTPException
    job=ANALYSIS_JOBS.get(job_id)
    if not job or job.get("owner") != request.state.username:
        raise HTTPException(status_code=404,detail="Processamento não encontrado ou expirado.")
    response={k:v for k,v in job.items() if k not in {"result", "owner"}}
    if job.get("status")=="done":
        response["resultado"]=job.get("result")
    return response


@app.get("/documento/{session_id}/{document_id}")
async def documento(session_id:str,document_id:str, request: Request = None):
    from fastapi import HTTPException
    session=REVISION_SESSIONS.get(session_id)
    if not session or (request and session.get("owner") != request.state.username): raise HTTPException(status_code=404,detail="Sessão de revisão expirada.")
    doc=session["documents"].get(document_id)
    if not doc: raise HTTPException(status_code=404,detail="Documento não encontrado.")
    original_name=str(doc.get("filename") or "documento.pdf").replace("\r", " ").replace("\n", " ")
    original_name=original_name.replace("–", "-").replace("—", "-")
    ascii_name=unicodedata.normalize("NFKD",original_name).encode("ascii","ignore").decode("ascii")
    ascii_name=re.sub(r'[^A-Za-z0-9._() -]+','_',ascii_name).strip() or "documento.pdf"


    disposition=f'inline; filename="{ascii_name}"'
    return StreamingResponse(
        io.BytesIO(doc["bytes"]),
        media_type="application/pdf",
        headers={"Content-Disposition":disposition},
    )


@app.post("/exportar")
async def exportar(request:ExportRequest, http_request: Request = None):
    _limpar_sessoes_expiradas()
    from fastapi import HTTPException
    if request.session_id not in REVISION_SESSIONS or (http_request and REVISION_SESSIONS[request.session_id].get("owner") != http_request.state.username):
        raise HTTPException(status_code=404,detail="Sessão de revisão expirada. Reprocesse os documentos.")
    nfe_rows=_sanitizar_export_rows(request.nfe_rows)
    nfse_rows=_sanitizar_export_rows(request.nfse_rows)
    cte_rows=_sanitizar_export_rows(request.cte_rows)
    outros_rows=_sanitizar_export_rows(request.outros_rows)
    orc_rows=_sanitizar_export_rows(request.orcamento_rows)
    if not nfe_rows:nfe_rows=[{"Aviso":"Nenhuma NF-e processada"}]
    if not nfse_rows:nfse_rows=[{"Aviso":"Nenhuma NFS-e processada"}]
    if not cte_rows:cte_rows=[{"Aviso":"Nenhum CT-e processado"}]
    if not outros_rows:outros_rows=[{"Aviso":"Nenhum documento enviado para Outros"}]
    if not orc_rows:orc_rows=[{"Aviso":"Nenhum orçamento processado"}]
    buf=io.BytesIO()
    with pd.ExcelWriter(buf,engine="openpyxl") as writer:
        pd.DataFrame(nfe_rows).to_excel(writer,index=False,sheet_name="Produtos (NF-e)" )
        pd.DataFrame(nfse_rows).to_excel(writer,index=False,sheet_name="Serviços (NFS-e)")
        pd.DataFrame(cte_rows).to_excel(writer,index=False,sheet_name="Transportes (CT-e)")
        pd.DataFrame(outros_rows).to_excel(writer,index=False,sheet_name="Outros")
        pd.DataFrame(orc_rows).to_excel(writer,index=False,sheet_name="Orçamentos")
        style_workbook(writer)
    buf.seek(0)
    return StreamingResponse(buf,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",headers={"Content-Disposition":"attachment; filename=Resultado_NFreader.xlsx"})


@app.post("/processar")
async def processar_notas(request: Request, centroCusto:str=Form(...),notas:UploadFile=File(...)):
    session_response=await analisar_notas(request=request,centroCusto=centroCusto,modo="normal",notas=notas)
    return await exportar(ExportRequest(session_id=session_response["session_id"],centro_custo=centroCusto,nfe_rows=session_response["nfe_rows"],nfse_rows=session_response["nfse_rows"],cte_rows=session_response["cte_rows"],outros_rows=session_response["outros_rows"],orcamento_rows=[]), http_request=request)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
