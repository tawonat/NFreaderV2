"""Leitura fiscal multimodal usando a Gemini Developer API.

O módulo concentra a dependência externa e devolve um contrato estável para o
restante do NFreader. Nenhuma chave é armazenada no código-fonte.
"""
from __future__ import annotations

import base64
from collections import deque
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter
import io


class GeminiError(RuntimeError):
    """Erro apresentável ao usuário durante a leitura fiscal."""


class RetryablePageError(GeminiError):
    """Falha transitória: a página pode voltar para o fim da fila."""


def _load_local_env() -> None:
    """Carrega somente variáveis ausentes do arquivo ``back/.env``."""
    path = Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    try:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                os.environ.setdefault(key, value)
    except OSError:
        pass


def _page_count(pdf_bytes: bytes) -> int:
    try:
        return max(1, len(PdfReader(io.BytesIO(pdf_bytes)).pages))
    except Exception as exc:
        raise GeminiError(f"PDF inválido ou corrompido: {exc}") from exc


DOCUMENT_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "documents": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "pages": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                    "document_type": {
                        "type": "STRING",
                        "enum": ["NFE", "NFSE", "CTE", "BOLETO", "OUTRO"],
                    },
                    "confidence": {"type": "NUMBER"},
                    "is_continuation": {"type": "BOOLEAN"},
                    "number": {"type": "STRING"},
                    "provider_name": {"type": "STRING"},
                    "provider_cnpj": {"type": "STRING"},
                    "customer_name": {"type": "STRING"},
                    "total_value": {"type": "STRING"},
                    "service_description": {"type": "STRING"},
                    "service_code": {"type": "STRING"},
                    "service_code_reason": {"type": "STRING"},
                    "items": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "product_code": {"type": "STRING"},
                                "description": {"type": "STRING"},
                                "quantity": {"type": "STRING"},
                                "ncm": {"type": "STRING"},
                                "unit_value": {"type": "STRING"},
                                "total_value": {"type": "STRING"},
                            },
                            "required": ["product_code", "description", "quantity", "ncm", "unit_value", "total_value"],
                        },
                    },
                    "evidence": {
                        "type": "OBJECT",
                        "properties": {
                            "number": {"type": "STRING"},
                            "provider": {"type": "STRING"},
                            "total": {"type": "STRING"},
                            "description": {"type": "STRING"},
                        },
                        "required": ["number", "provider", "total", "description"],
                    },
                    "notes": {"type": "STRING"},
                },
                "required": [
                    "pages", "document_type", "confidence", "is_continuation", "number",
                    "provider_name", "provider_cnpj", "customer_name",
                    "total_value", "service_description", "service_code",
                    "service_code_reason", "items", "evidence", "notes",
                ],
            },
        }
    },
    "required": ["documents"],
}


SYSTEM_PROMPT = """Você é o motor de leitura fiscal do NFreader. Analise visualmente a página do PDF brasileiro anexado.

OBJETIVOS
1. Determine se esta página inicia um documento ou continua o documento imediatamente anterior informado no contexto.
2. Classifique cada documento como NFE (mercadorias/DANFE), NFSE (serviços), CTE, BOLETO ou OUTRO.
3. Extraia exatamente o que está impresso. Nunca invente nem complete um campo ilegível.

REGRAS CRÍTICAS
- As páginas são numeradas a partir de 1 e chegam na orientação correta.
- Retorne exatamente um documento contendo a página local 1 em `pages`.
- `is_continuation` só pode ser verdadeiro quando a página claramente continua o documento anterior: tabela continuada, mesmos identificadores ou ausência de novo cabeçalho com conteúdo complementar.
- Um novo cabeçalho fiscal completo ou número diferente exige `is_continuation=false`.
- Em NFSE, `provider_name` é SEMPRE o PRESTADOR/EMITENTE, nunca o tomador/adquirente.
- Em NFE, `provider_name` é o EMITENTE da nota, nunca destinatário/transportadora.
- `number` é o número da nota, não RPS, série, pedido, protocolo, inscrição ou código de verificação.
- `total_value` deve ser o valor final da nota. Em NFS-e, prefira Valor Líquido quando ele estiver explicitamente apresentado como o total efetivo.
- Em NFSE, coloque em `service_description` toda a discriminação útil do serviço, sem cabeçalhos, impostos ou textos do tomador.
- Em NFSE, escolha em `service_code` exatamente um código do CATÁLOGO DE SERVIÇOS fornecido. Interprete semanticamente o serviço completo; o texto da nota não precisa repetir literalmente a descrição do cadastro. Com descrição legível, escolha a categoria funcional mais próxima. Use string vazia somente quando o serviço estiver ausente/ilegível ou realmente fora de todas as categorias.
- Em `service_code_reason`, explique em uma frase curta qual trecho da nota sustenta a escolha.
- Em `service_code_reason` e `notes`, descreva somente o documento e a leitura. Nunca mencione IA, Gemini, modelo, API ou tecnologia utilizada.
- Em NFE, extraia todos os produtos em `items`, preservando código do produto, descrição, quantidade e NCM.
- Em `quantity`, copie como texto exatamente os algarismos e separadores impressos na coluna QUANTIDADE de cada item. Preserve vírgula ou ponto decimal, zeros à esquerda e zeros à direita: `8.0000` deve continuar `8.0000` e `0,0080` deve continuar `0,0080`. Nunca arredonde, converta em número, calcule ou use a quantidade de outra coluna. Se não estiver legível, use string vazia.
- Em `product_code`, copie exatamente o código do item impresso pelo emitente/fornecedor nas colunas como "CÓD. PRODUTO" ou "Código Produto". Esse não é o código interno do banco de dados. Nunca use NCM, CFOP, CST, quantidade ou número da nota como `product_code`.
- Para NFS-e, `items` deve ser vazio. Para NFE, `service_description` deve ser vazio.
- Valores monetários usam formato brasileiro, por exemplo `2.730,62`, sem `R$`.
- Campos ausentes ou ilegíveis devem ser string vazia.
- `confidence` vai de 0 a 100 e mede somente a confiança na leitura deste documento.
- Em `evidence`, copie trechos curtos realmente visíveis que sustentem cada campo. Se não houver evidência, use string vazia.
- Não use conhecimento externo e não calcule valores que não estejam impressos.
"""


def _extract_json(response: dict[str, Any]) -> dict[str, Any]:
    text = ""

    for step in response.get("steps", []) if isinstance(response.get("steps"), list) else []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        content = step.get("content", [])
        if isinstance(content, list):
            text += "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )

    if not text.strip():
        try:
            candidates = response["candidates"]
            if not candidates:
                raise KeyError("candidates")
            parts = candidates[0]["content"]["parts"]
            text = "".join(str(part.get("text", "")) for part in parts)
        except (KeyError, IndexError, TypeError):
            text = ""
    text = text.strip()
    if not text:
        block = response.get("promptFeedback", {}).get("blockReason", "")
        suffix = f" Motivo: {block}." if block else ""
        raise GeminiError(f"O serviço de leitura não retornou uma análise válida.{suffix}")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeminiError("O serviço de leitura retornou uma resposta inválida.") from exc
    if not isinstance(parsed, dict):
        raise GeminiError("O serviço de leitura retornou um formato de análise inesperado.")
    return parsed


def _clean_text(value: Any, fallback: str = "") -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or fallback


def _public_text(value: Any, fallback: str = "") -> str:
    """Remove referências ao motor de textos que podem aparecer na interface."""
    text = _clean_text(value, fallback)
    text = re.sub(r"(?i)\bGemini(?:-[A-Za-z0-9.-]+)?\b", "serviço de leitura", text)
    text = re.sub(r"(?i)\bintelig[êe]ncia artificial\b", "leitura automática", text)
    text = re.sub(r"(?i)\bIA\b", "leitura automática", text)
    return re.sub(r"\s+", " ", text).strip()


def _standard_json_schema(value: Any) -> Any:
    """Converte os tipos do schema Google antigo para JSON Schema padrão."""
    if isinstance(value, dict):
        return {
            key: (str(item).lower() if key == "type" and isinstance(item, str) else _standard_json_schema(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_standard_json_schema(item) for item in value]
    return value


def _document_schema_for_codes(service_codes: list[str]) -> dict[str, Any]:
    """Restringe a saída aos códigos realmente existentes no cadastro carregado."""
    schema = json.loads(json.dumps(DOCUMENT_SCHEMA))
    code_schema = schema["properties"]["documents"]["items"]["properties"]["service_code"]
    code_schema["enum"] = ["", *service_codes]
    return schema


def _money(value: Any) -> str:
    raw = _clean_text(value).replace("R$", "").replace(" ", "")
    if not raw:
        return "-"
    match = re.search(r"(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}", raw)
    if match:
        return match.group(0)

    try:
        return f"{float(raw):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    except ValueError:
        return "-"


def normalize_analysis(raw: dict[str, Any], total_pages: int) -> list[dict[str, Any]]:
    """Sanitiza a saída e garante que toda página pertença a um único grupo."""
    documents = raw.get("documents", [])
    if not isinstance(documents, list):
        raise GeminiError("A resposta da leitura não contém a lista de documentos.")
    claimed: set[int] = set()
    normalized: list[dict[str, Any]] = []
    valid_types = {"NFE", "NFSE", "CTE", "BOLETO", "OUTRO"}
    for source in documents:
        if not isinstance(source, dict):
            continue
        pages: list[int] = []
        for value in source.get("pages", []):
            try:
                page = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= page <= total_pages and page not in claimed:
                pages.append(page)
                claimed.add(page)
        if not pages:
            continue
        doc_type = _clean_text(source.get("document_type")).upper().replace("-", "")
        if doc_type not in valid_types:
            doc_type = "OUTRO"
        try:
            confidence = max(0.0, min(100.0, float(source.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        items = []
        if isinstance(source.get("items"), list):
            for item in source["items"]:
                if not isinstance(item, dict) or not _clean_text(item.get("description")):
                    continue
                quantity = _clean_text(item.get("quantity"))
                items.append({
                    "product_code": _clean_text(item.get("product_code")),
                    "description": _clean_text(item.get("description")),
                    "quantity": quantity,
                    "ncm": re.sub(r"\D", "", _clean_text(item.get("ncm")))[:8],
                    "unit_value": _money(item.get("unit_value")),
                    "total_value": _money(item.get("total_value")),
                })
        normalized.append({
            "pages": sorted(pages),
            "document_type": doc_type,
            "confidence": confidence,
            "is_continuation": bool(source.get("is_continuation", False)),
            "number": _clean_text(source.get("number"), "SemNumero"),
            "provider_name": _clean_text(source.get("provider_name"), "Empresa não identificada"),
            "provider_cnpj": re.sub(r"\D", "", _clean_text(source.get("provider_cnpj")))[:14],
            "customer_name": _clean_text(source.get("customer_name")),
            "total_value": _money(source.get("total_value")),
            "service_description": _clean_text(source.get("service_description"), "Prestação de serviço"),
            "service_code": re.sub(r"\D", "", _clean_text(source.get("service_code")))[:12],
            "service_code_reason": _public_text(source.get("service_code_reason")),
            "items": items,
            "evidence": source.get("evidence") if isinstance(source.get("evidence"), dict) else {},
            "notes": _public_text(source.get("notes")),
        })
    for page in range(1, total_pages + 1):
        if page not in claimed:
            normalized.append({
                "pages": [page], "document_type": "OUTRO", "confidence": 0.0,
                "is_continuation": False,
                "number": "SemNumero", "provider_name": "Empresa não identificada",
                "provider_cnpj": "", "customer_name": "", "total_value": "-",
                "service_description": "", "items": [], "evidence": {},
                "service_code": "", "service_code_reason": "",
                "notes": "A leitura não atribuiu esta página a um documento.",
            })
    normalized.sort(key=lambda doc: doc["pages"][0])
    return normalized


def _split_pdf_pages(pdf_bytes: bytes) -> list[bytes]:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages: list[bytes] = []
        for page in reader.pages:
            writer = PdfWriter()
            writer.add_page(page)
            output = io.BytesIO()
            writer.write(output)
            pages.append(output.getvalue())
        return pages
    except Exception as exc:
        raise GeminiError(f"Não foi possível separar as páginas do PDF: {exc}") from exc


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        message = payload.get("error", {}).get("message", "")
    except Exception:
        message = ""
    if exc.code in {401, 403}:
        return "credencial inválida, sem permissão ou serviço indisponível para este projeto"
    if exc.code == 429:
        return "serviço temporariamente indisponível; a página será tentada novamente"
    return message or f"HTTP {exc.code}"


def _request_page(
    page_bytes: bytes,
    prompt: str,
    api_key: str,
    model: str,
    timeout: float,
    retry_callback=None,
    model_offset: int = 0,
    max_attempts_override: int | None = None,
    response_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        max_attempts = max(1, min(6, int(os.getenv("NFREADER_GEMINI_RETRIES", "4"))))
    except ValueError:
        max_attempts = 4
    if max_attempts_override is not None:
        max_attempts = max(1, min(6, int(max_attempts_override)))
    try:
        base_delay = max(1.0, float(os.getenv("NFREADER_GEMINI_RETRY_DELAY", "5")))
    except ValueError:
        base_delay = 5.0

    models = _model_candidates(model)
    encoded_page = base64.b64encode(page_bytes).decode("ascii")

    def notify_retry(attempt: int, delay: float, current_model: str, next_model: str) -> None:
        if not retry_callback:
            return
        try:
            retry_callback(attempt, max_attempts, delay, current_model, next_model)
        except Exception:

            pass

    for attempt in range(max_attempts):
        current_model = models[(model_offset + attempt) % len(models)]
        body = {
            "model": current_model,
            "input": [
                {
                    "type": "document",
                    "data": encoded_page,
                    "mime_type": "application/pdf",
                },
                {"type": "text", "text": prompt},
            ],
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": _standard_json_schema(response_schema or DOCUMENT_SCHEMA),
            },
        }
        request = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as result:
                return json.loads(result.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            transient = exc.code in {429, 500, 502, 503, 504}
            if transient and attempt + 1 < max_attempts:
                delay = min(60.0, base_delay * (2 ** attempt))
                next_model = models[(model_offset + attempt + 1) % len(models)]
                notify_retry(attempt + 1, delay, current_model, next_model)
                time.sleep(delay)
                continue
            detail = _http_error_detail(exc)
            if transient:
                raise RetryablePageError(
                    f"Serviço de leitura temporariamente indisponível após {attempt + 1} tentativa(s): {detail}."
                ) from exc
            raise GeminiError(f"Falha no serviço de leitura: {detail}.") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            if attempt + 1 < max_attempts:
                delay = min(60.0, base_delay * (2 ** attempt))
                next_model = models[(model_offset + attempt + 1) % len(models)]
                notify_retry(attempt + 1, delay, current_model, next_model)
                time.sleep(delay)
                continue
            raise RetryablePageError(
                f"Não foi possível acessar o serviço de leitura após {attempt + 1} tentativa(s)."
            ) from exc
        except json.JSONDecodeError as exc:
            raise RetryablePageError("O serviço de leitura retornou uma resposta inválida.") from exc
    raise RetryablePageError("Não foi possível concluir a leitura desta página.")


def _queue_retry_delays() -> list[float]:
    raw = os.getenv("NFREADER_QUEUE_RETRY_DELAYS", "30,60,120,180")
    delays: list[float] = []
    for value in raw.split(","):
        try:
            delays.append(max(0.0, float(value.strip())))
        except ValueError:
            continue
    return delays or [30.0, 60.0, 120.0, 180.0]


def _model_candidates(model: str) -> list[str]:
    fallback_models = [
        candidate.strip().removeprefix("models/")
        for candidate in os.getenv(
            "GEMINI_FALLBACK_MODELS",
            "gemini-3.6-flash,gemini-3.5-flash",
        ).split(",")
        if candidate.strip()
    ]
    return list(dict.fromkeys([model.removeprefix("models/"), *fallback_models]))


def _request_service_code(
    document: dict[str, Any],
    catalog_text: str,
    service_codes: list[str],
    api_key: str,
    model: str,
    timeout: float,
    model_offset: int = 0,
) -> tuple[str, str]:
    """Segunda leitura textual focada apenas na classificação do serviço."""
    schema = {
        "type": "object",
        "properties": {
            "service_code": {"type": "string", "enum": ["", *service_codes]},
            "reason": {"type": "string"},
        },
        "required": ["service_code", "reason"],
    }
    evidence = document.get("evidence") if isinstance(document.get("evidence"), dict) else {}
    prompt = f"""Classifique uma NFS-e em exatamente uma categoria do catálogo fechado abaixo.

DADOS EXTRAÍDOS DA NOTA
Prestador: {document.get('provider_name', '')}
Descrição integral do serviço: {document.get('service_description', '')}
Evidência visual da descrição: {evidence.get('description', '')}
Observações: {document.get('notes', '')}

CATÁLOGO PERMITIDO
{catalog_text}

REGRAS
- Retorne em service_code somente um código listado no catálogo.
- Faça correspondência semântica: o texto da nota não precisa repetir literalmente a descrição do cadastro.
- Calibração, aferição ou atividade metrológica pertence ao serviço de calibração.
- Use assistência técnica hora-homem somente quando houver cobrança por horas, HH, mão de obra ou técnico por período.
- Reparos, consertos, manutenção, usinagem e fabricação técnica sem cobrança explícita por hora pertencem à assistência técnica comum.
- Em locações, escolha o equipamento específico quando houver categoria específica; use locação de equipamentos de construção apenas quando nenhuma locação específica servir.
- Transporte e destinação de resíduos são categorias diferentes; priorize a finalidade principal descrita na nota.
- Use string vazia somente se a descrição estiver ausente/ilegível ou não houver nenhuma categoria razoavelmente compatível.
- Em reason, explique brevemente a evidência documental. Não cite tecnologia, modelo ou fornecedor.
"""
    models = _model_candidates(model)
    current_model = models[model_offset % len(models)]
    body = {
        "model": current_model,
        "input": [{"type": "text", "text": prompt}],
        "response_format": {
            "type": "text", "mime_type": "application/json",
            "schema": schema,
        },
    }
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as result:
            parsed = _extract_json(json.loads(result.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        if exc.code in {429, 500, 502, 503, 504}:
            raise RetryablePageError("Classificação temporariamente indisponível.") from exc
        raise GeminiError(f"Falha no serviço de leitura: {_http_error_detail(exc)}.") from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, json.JSONDecodeError, GeminiError) as exc:
        raise RetryablePageError("Não foi possível concluir a classificação do serviço.") from exc
    code = re.sub(r"\D", "", _clean_text(parsed.get("service_code")))[:12]
    if code not in service_codes:
        code = ""
    return code, _public_text(parsed.get("reason"))


def _identity_value(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _clean_text(value).upper())


def _should_merge(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    fiscal = {"NFE", "NFSE", "CTE"}
    if previous.get("document_type") not in fiscal or current.get("document_type") not in fiscal:
        return False
    if previous.get("document_type") != current.get("document_type"):
        return False
    if current.get("is_continuation"):
        return True
    prev_number = _identity_value(previous.get("number"))
    cur_number = _identity_value(current.get("number"))
    invalid = {"", "SEMNUMERO"}
    if prev_number in invalid or cur_number in invalid or prev_number != cur_number:
        return False
    prev_provider = _identity_value(previous.get("provider_name"))
    cur_provider = _identity_value(current.get("provider_name"))
    return not prev_provider or not cur_provider or prev_provider == cur_provider


def _merge_document(previous: dict[str, Any], current: dict[str, Any]) -> None:
    previous["pages"] = sorted(set(previous.get("pages", []) + current.get("pages", [])))
    previous["confidence"] = round(
        (float(previous.get("confidence", 0)) + float(current.get("confidence", 0))) / 2, 1
    )
    missing = {"", "-", "SemNumero", "Empresa não identificada", "Prestação de serviço"}
    for field in [
        "number", "provider_name", "provider_cnpj", "customer_name", "total_value",
        "service_code", "service_code_reason",
    ]:
        if previous.get(field) in missing and current.get(field) not in missing:
            previous[field] = current[field]
    current_description = _clean_text(current.get("service_description"))
    if current_description and current_description not in {"Prestação de serviço", previous.get("service_description")}:
        old_description = _clean_text(previous.get("service_description"))
        if old_description in {"", "Prestação de serviço"}:
            previous["service_description"] = current_description
        else:
            previous["service_description"] = f"{old_description}; {current_description}"
    previous["items"].extend(current.get("items", []))
    for key, value in current.get("evidence", {}).items():
        if value and not previous.get("evidence", {}).get(key):
            previous.setdefault("evidence", {})[key] = value
    if current.get("notes"):
        previous["notes"] = "; ".join(
            dict.fromkeys(x for x in [previous.get("notes", ""), current["notes"]] if x)
        )


def _reading_settings() -> tuple[str, str, float, float]:
    _load_local_env()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise GeminiError("O serviço de leitura não está configurado. Execute o configurador antes de iniciar o backend.")
    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"


    if model in {"gemini-2.5-flash", "models/gemini-2.5-flash"}:
        model = "gemini-3.5-flash-lite"
    try:
        timeout = max(20.0, float(os.getenv("NFREADER_GEMINI_TIMEOUT", "90")))
    except ValueError:
        timeout = 90.0
    try:
        page_delay = max(0.0, float(os.getenv("NFREADER_GEMINI_PAGE_DELAY", "8")))
    except ValueError:
        page_delay = 8.0
    return api_key, model, timeout, page_delay


def _service_catalog_text(service_catalog: list[dict[str, Any]] | None) -> str:
    catalog_lines = []
    for row in service_catalog or []:
        code = _clean_text(row.get("codigo"))
        description = _clean_text(row.get("descricao_extra") or row.get("descricao"))
        if code and description:
            catalog_lines.append(f"- {code}: {description}")
    return "\n".join(catalog_lines) or "- Nenhum catálogo disponível; deixe service_code vazio."


def _service_codes(service_catalog: list[dict[str, Any]] | None) -> list[str]:
    return list(dict.fromkeys(
        re.sub(r"\D", "", _clean_text(row.get("codigo")))[:12]
        for row in service_catalog or []
        if re.sub(r"\D", "", _clean_text(row.get("codigo")))
    ))


def _assemble_documents(page_results: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for page_number in sorted(page_results):
        current = page_results[page_number]
        if documents and _should_merge(documents[-1], current):
            _merge_document(documents[-1], current)
        else:
            current["is_continuation"] = False
            documents.append(current)
    return documents


def _pdf_page_worker(
    page_files: list[bytes],
    filename: str,
    catalog_text: str,
    service_codes: list[str],
    api_key: str,
    model: str,
    timeout: float,
):
    """Gerador cooperativo: processa no máximo uma tentativa antes de devolver o controle."""
    total_pages = len(page_files)
    page_results: dict[int, dict[str, Any]] = {}
    pending_documents: dict[int, dict[str, Any]] = {}
    page_failures: dict[int, int] = {}
    next_attempt_at: dict[int, float] = {}
    queue = deque(enumerate(page_files, start=1))
    retry_delays = _queue_retry_delays()
    try:
        max_queue_attempts = max(0, int(os.getenv("NFREADER_QUEUE_MAX_ATTEMPTS", "0")))
    except ValueError:
        max_queue_attempts = 0

    while queue:
        now = time.monotonic()
        ready = None
        for _ in range(len(queue)):
            candidate = queue.popleft()
            if next_attempt_at.get(candidate[0], 0.0) <= now and ready is None:
                ready = candidate
            else:
                queue.append(candidate)
        if ready is None:
            wait_seconds = max(
                0.0,
                min(next_attempt_at.get(number, now) for number, _ in queue) - now,
            )
            yield {
                "kind": "waiting", "done": len(page_results), "total": total_pages,
                "wait": min(15.0, wait_seconds), "page": None,
            }
            continue

        page_number, page_bytes = ready

        previous_context = "Não há documento anterior."
        previous = page_results.get(page_number - 1)
        if previous:
            previous_context = (
                "Página imediatamente anterior: "
                f"tipo={previous.get('document_type')}; número={previous.get('number')}; "
                f"prestador/emitente={previous.get('provider_name')}; "
                f"páginas globais={previous.get('pages')}."
            )
        elif page_number > 1:
            previous_context = (
                "A página imediatamente anterior ainda não está disponível. "
                "Decida visualmente se esta página parece iniciar um documento ou ser continuação."
            )
        prompt = (
            f"Arquivo: {filename}. Esta é a página global {page_number} de {total_pages}.\n"
            f"{previous_context}\n\n{SYSTEM_PROMPT}\n\n"
            f"CATÁLOGO DE SERVIÇOS PERMITIDOS:\n{catalog_text}"
        )
        try:
            current = pending_documents.get(page_number)
            if current is None:
                response = _request_page(
                    page_bytes,
                    prompt,
                    api_key,
                    model,
                    timeout,
                    model_offset=page_failures.get(page_number, 0),
                    max_attempts_override=1,
                    response_schema=_document_schema_for_codes(service_codes),
                )
                try:
                    local_documents = normalize_analysis(_extract_json(response), 1)
                except GeminiError as exc:
                    raise RetryablePageError(str(exc)) from exc
                current = local_documents[0]
            current_code = re.sub(r"\D", "", _clean_text(current.get("service_code")))[:12]
            if (
                current.get("document_type") == "NFSE"
                and service_codes
                and current_code not in service_codes
            ):
                pending_documents[page_number] = current
                code, reason = _request_service_code(
                    current,
                    catalog_text,
                    service_codes,
                    api_key,
                    model,
                    timeout,
                    model_offset=page_failures.get(page_number, 0),
                )
                current["service_code"] = code
                current["service_code_reason"] = reason
        except RetryablePageError as exc:
            failures = page_failures.get(page_number, 0) + 1
            page_failures[page_number] = failures
            if max_queue_attempts and failures >= max_queue_attempts:
                raise GeminiError(
                    f"A página {page_number} não pôde ser lida após {failures} tentativas."
                ) from exc
            delay = retry_delays[min(failures - 1, len(retry_delays) - 1)]
            next_attempt_at[page_number] = time.monotonic() + delay
            queue.append((page_number, page_bytes))
            now = time.monotonic()
            has_ready_page = any(next_attempt_at.get(number, 0.0) <= now for number, _ in queue)
            yield {
                "kind": "deferred", "done": len(page_results), "total": total_pages,
                "wait": 0.0 if has_ready_page else min(15.0, delay), "page": page_number,
            }
            continue

        current["pages"] = [page_number]
        page_results[page_number] = current
        pending_documents.pop(page_number, None)
        next_attempt_at.pop(page_number, None)
        yield {
            "kind": "completed", "done": len(page_results), "total": total_pages,
            "wait": 0.0, "page": page_number,
        }

    return _assemble_documents(page_results)


def analyze_fiscal_pdf(
    pdf_bytes: bytes,
    filename: str = "documento.pdf",
    progress_callback=None,
    service_catalog: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Lê um PDF com fila resiliente de páginas e preserva o contrato anterior."""
    api_key, model, timeout, page_delay = _reading_settings()
    if len(pdf_bytes) > 50 * 1024 * 1024:
        raise GeminiError("O PDF excede o limite de 50 MB aceito pelo serviço de leitura.")
    page_files = _split_pdf_pages(pdf_bytes)
    service_codes = _service_codes(service_catalog)
    worker = _pdf_page_worker(
        page_files, filename, _service_catalog_text(service_catalog), service_codes,
        api_key, model, timeout
    )
    while True:
        try:
            event = next(worker)
        except StopIteration as completed:
            return completed.value
        done, total = event["done"], event["total"]
        if event["kind"] == "deferred":
            message = (
                f"Página {event['page']} aguardando disponibilidade; movida para o fim da fila. "
                f"{done} de {total} páginas concluídas."
            )
        elif event["kind"] == "waiting":
            message = (
                f"{done} de {total} páginas concluídas. "
                f"Retomando páginas pendentes em até {math.ceil(event['wait'])}s."
            )
        else:
            message = f"Leitura em andamento: {done} de {total} páginas concluídas."
        if progress_callback:
            progress_callback(done, total, message)
        if event["kind"] == "waiting" and event["wait"]:
            time.sleep(event["wait"])
        elif event["kind"] == "completed" and page_delay:
            time.sleep(page_delay)


def analyze_fiscal_pdfs(
    files: list[tuple[str, bytes]],
    progress_callback=None,
    service_catalog: list[dict[str, Any]] | None = None,
) -> list[tuple[str, bytes, list[dict[str, Any]]]]:
    """Processa vários PDFs em round-robin, sem deixar um arquivo bloquear os demais."""
    api_key, model, timeout, page_delay = _reading_settings()
    catalog_text = _service_catalog_text(service_catalog)
    service_codes = _service_codes(service_catalog)
    states = []
    total_pages = 0
    for index, (filename, pdf_bytes) in enumerate(files):
        if len(pdf_bytes) > 50 * 1024 * 1024:
            raise GeminiError(f"O arquivo {filename} excede o limite de 50 MB do serviço de leitura.")
        page_files = _split_pdf_pages(pdf_bytes)
        total_pages += len(page_files)
        states.append({
            "index": index, "filename": filename, "bytes": pdf_bytes,
            "worker": _pdf_page_worker(
                page_files, filename, catalog_text, service_codes, api_key, model, timeout
            ),
            "last_wait": 0.0,
        })

    queue = deque(states)
    results: list[list[dict[str, Any]] | None] = [None] * len(states)
    completed_pages = 0
    completed_files = 0
    events_without_progress = 0

    while queue:
        state = queue.popleft()
        try:
            event = next(state["worker"])
        except StopIteration as completed:
            results[state["index"]] = completed.value
            completed_files += 1
            events_without_progress = 0
            if progress_callback:
                progress_callback(
                    completed_pages,
                    total_pages,
                    f"Arquivo concluído: {state['filename']}. "
                    f"{completed_files} de {len(states)} arquivos finalizados.",
                )
            continue

        state["last_wait"] = float(event.get("wait") or 0.0)
        queue.append(state)
        if event["kind"] == "completed":
            completed_pages += 1
            events_without_progress = 0
            message = (
                f"Leitura em andamento: {completed_pages} de {total_pages} páginas concluídas "
                f"em {completed_files} de {len(states)} arquivos finalizados."
            )
        elif event["kind"] == "deferred":
            events_without_progress += 1
            message = (
                f"{state['filename']}: página {event['page']} aguardando; "
                f"arquivo movido para o fim da fila. {completed_pages} de {total_pages} páginas concluídas."
            )
        else:
            events_without_progress += 1
            message = (
                f"{completed_pages} de {total_pages} páginas concluídas. "
                "Verificando o próximo arquivo da fila."
            )
        if progress_callback:
            progress_callback(completed_pages, total_pages, message)

        if event["kind"] == "completed" and page_delay:
            time.sleep(page_delay)
        elif queue and events_without_progress >= len(queue):
            waits = [float(item.get("last_wait") or 0.0) for item in queue]
            if waits and all(wait > 0 for wait in waits):
                wait_seconds = min(15.0, min(waits))
                if progress_callback:
                    progress_callback(
                        completed_pages,
                        total_pages,
                        f"{completed_pages} de {total_pages} páginas concluídas. "
                        f"Retomando arquivos pendentes em até {math.ceil(wait_seconds)}s.",
                    )
                if wait_seconds:
                    time.sleep(wait_seconds)
            events_without_progress = 0

    return [
        (filename, pdf_bytes, results[index] or [])
        for index, (filename, pdf_bytes) in enumerate(files)
    ]
