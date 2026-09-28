import json
import os
import sys
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter


BACK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACK_DIR))

import gemini_engine  # noqa: E402


class GeminiEngineTests(unittest.TestCase):
    @staticmethod
    def _pdf_with_pages(total):
        writer = PdfWriter()
        for _ in range(total):
            writer.add_blank_page(width=595, height=842)
        output = BytesIO()
        writer.write(output)
        return output.getvalue()

    def test_missing_api_key_has_actionable_error(self):
        old = os.environ.pop("GEMINI_API_KEY", None)
        try:
            with self.assertRaisesRegex(gemini_engine.GeminiError, "configurador"):
                gemini_engine.analyze_fiscal_pdf(b"%PDF-1.4\n%%EOF")
        finally:
            if old is not None:
                os.environ["GEMINI_API_KEY"] = old

    def test_extracts_structured_json_from_api_response(self):
        response = {
            "candidates": [{
                "content": {"parts": [{"text": "```json\n{\"documents\": []}\n```"}]}
            }]
        }
        self.assertEqual(gemini_engine._extract_json(response), {"documents": []})

    def test_extracts_json_from_interactions_response(self):
        response = {"steps": [{
            "type": "model_output",
            "content": [{"type": "text", "text": '{"documents": []}'}],
        }]}
        self.assertEqual(gemini_engine._extract_json(response), {"documents": []})

    def test_normalization_preserves_provider_and_fiscal_fields(self):
        raw = {"documents": [{
            "pages": [1],
            "document_type": "NFSE",
            "confidence": 96,
            "number": "57684",
            "provider_name": "VERTIV TECNOLOGIA DO BRASIL LTDA.",
            "provider_cnpj": "03.698.721/0001-16",
            "customer_name": "ZD ALIMENTOS S.A.",
            "total_value": "R$ 2.730,62",
            "service_description": "Manutenção preventiva industrial",
            "service_code": "750027",
            "service_code_reason": "A IA identificou manutenção.",
            "items": [],
            "evidence": {"provider": "Nome/Razão Social VERTIV TECNOLOGIA"},
            "notes": "Gemini confirmou os campos.",
        }]}
        result = gemini_engine.normalize_analysis(raw, 1)[0]
        self.assertEqual(result["number"], "57684")
        self.assertEqual(result["provider_name"], "VERTIV TECNOLOGIA DO BRASIL LTDA.")
        self.assertNotEqual(result["provider_name"], result["customer_name"])
        self.assertEqual(result["total_value"], "2.730,62")
        self.assertNotIn("IA", result["service_code_reason"].upper().split())
        self.assertNotIn("GEMINI", result["notes"].upper())

    def test_duplicate_and_unassigned_pages_are_handled_safely(self):
        raw = {"documents": [
            {"pages": [1, 2], "document_type": "NFSE", "confidence": 90},
            {"pages": [2, 3], "document_type": "NFE", "confidence": 80},
        ]}
        result = gemini_engine.normalize_analysis(raw, 4)
        all_pages = [page for document in result for page in document["pages"]]
        self.assertEqual(sorted(all_pages), [1, 2, 3, 4])
        self.assertEqual(len(all_pages), len(set(all_pages)))
        self.assertEqual(result[-1]["document_type"], "OUTRO")

    def test_schema_requires_evidence_and_page_assignment(self):
        item = gemini_engine.DOCUMENT_SCHEMA["properties"]["documents"]["items"]
        self.assertIn("pages", item["required"])
        self.assertIn("evidence", item["required"])
        self.assertIn("provider_name", item["properties"])
        self.assertIn("is_continuation", item["required"])
        self.assertIn("service_code", item["required"])
        self.assertIn("service_code_reason", item["required"])
        product_item = item["properties"]["items"]["items"]
        self.assertIn("product_code", product_item["required"])

    def test_normalization_preserves_issuer_product_code_exactly(self):
        raw = {"documents": [{
            "pages": [1], "document_type": "NFE", "confidence": 98,
            "number": "123", "provider_name": "FORNECEDOR LTDA",
            "items": [{
                "product_code": "ABC-001/XP", "description": "Peça industrial",
                "quantity": 2, "ncm": "8481.80.99", "unit_value": "10,00",
                "total_value": "20,00",
            }],
        }]}
        item = gemini_engine.normalize_analysis(raw, 1)[0]["items"][0]
        self.assertEqual(item["product_code"], "ABC-001/XP")
        self.assertEqual(item["ncm"], "84818099")

    def test_normalization_preserves_printed_quantity_text(self):
        raw = {"documents": [{
            "pages": [1], "document_type": "NFE", "items": [
                {"description": "Peça", "quantity": "8.0000"},
                {"description": "Produto fracionado", "quantity": "0,0080"},
            ],
        }]}
        quantities = [item["quantity"] for item in gemini_engine.normalize_analysis(raw, 1)[0]["items"]]
        self.assertEqual(quantities, ["8.0000", "0,0080"])
        item_schema = gemini_engine.DOCUMENT_SCHEMA["properties"]["documents"]["items"]["properties"]["items"]["items"]
        self.assertEqual(item_schema["properties"]["quantity"]["type"], "STRING")

    def test_page_requests_are_merged_when_second_page_is_continuation(self):
        first = {"documents": [{
            "pages": [1], "document_type": "NFSE", "confidence": 96,
            "is_continuation": False, "number": "57684",
            "provider_name": "VERTIV TECNOLOGIA DO BRASIL LTDA",
            "total_value": "2.730,62", "service_description": "Manutenção preventiva",
            "service_code": "750685", "service_code_reason": "Manutenção por hora-homem",
            "items": [], "evidence": {}, "notes": "",
        }]}
        second = {"documents": [{
            "pages": [1], "document_type": "NFSE", "confidence": 92,
            "is_continuation": True, "number": "57684",
            "provider_name": "VERTIV TECNOLOGIA DO BRASIL LTDA",
            "total_value": "", "service_description": "Complemento do serviço",
            "service_code": "", "service_code_reason": "",
            "items": [], "evidence": {}, "notes": "continuação",
        }]}
        responses = [
            {"steps": [{"type": "model_output", "content": [{"type": "text", "text": json.dumps(first)}]}]},
            {"steps": [{"type": "model_output", "content": [{"type": "text", "text": json.dumps(second)}]}]},
        ]
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "test-key", "NFREADER_GEMINI_PAGE_DELAY": "0"
        }), patch.object(gemini_engine, "_request_page", side_effect=responses) as request_page:
            result = gemini_engine.analyze_fiscal_pdf(self._pdf_with_pages(2), "duas-paginas.pdf")
        self.assertEqual(request_page.call_count, 2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pages"], [1, 2])
        self.assertIn("Complemento do serviço", result[0]["service_description"])
        self.assertEqual(result[0]["service_code"], "750685")

    def test_transient_503_is_retried_automatically(self):
        error = urllib.error.HTTPError(
            "https://example.test", 503, "Unavailable", {},
            BytesIO(b'{"error":{"message":"high demand"}}'),
        )

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"steps":[]}'

        retries = []
        with patch.dict(os.environ, {
            "NFREADER_GEMINI_RETRIES": "2", "NFREADER_GEMINI_RETRY_DELAY": "1",
            "GEMINI_FALLBACK_MODELS": "gemini-3.6-flash",
        }), patch.object(
            gemini_engine.urllib.request, "urlopen", side_effect=[error, FakeResponse()]
        ) as urlopen, patch.object(gemini_engine.time, "sleep") as sleep:
            response = gemini_engine._request_page(
                b"%PDF", "teste", "key", "gemini-3.5-flash-lite", 10,
                retry_callback=lambda *args: retries.append(args),
            )
        self.assertEqual(response, {"steps": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)
        self.assertEqual(
            retries,
            [(1, 2, 1.0, "gemini-3.5-flash-lite", "gemini-3.6-flash")],
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://generativelanguage.googleapis.com/v1beta/interactions",
        )
        body = json.loads(request.data.decode("utf-8"))
        first_body = json.loads(urlopen.call_args_list[0].args[0].data.decode("utf-8"))
        self.assertEqual(first_body["model"], "gemini-3.5-flash-lite")
        self.assertEqual(body["model"], "gemini-3.6-flash")
        self.assertIsInstance(body["response_format"], dict)
        self.assertEqual(body["response_format"]["schema"]["type"], "object")

    def test_unavailable_page_goes_to_end_of_queue_until_it_succeeds(self):
        call_order = []
        page_two_attempts = 0
        progress_messages = []

        def request_page(_bytes, prompt, *_args, **_kwargs):
            nonlocal page_two_attempts
            page = int(prompt.split("página global ", 1)[1].split(" ", 1)[0])
            call_order.append(page)
            if page == 2 and page_two_attempts == 0:
                page_two_attempts += 1
                raise gemini_engine.RetryablePageError("indisponível")
            number = "100" if page in {1, 2} else "300"
            payload = {"documents": [{
                "pages": [1], "document_type": "NFSE", "confidence": 95,
                "is_continuation": page == 2, "number": number,
                "provider_name": "EMPRESA TESTE LTDA", "provider_cnpj": "",
                "customer_name": "CLIENTE", "total_value": "100,00",
                "service_description": f"Serviço da página {page}",
                "service_code": "750027", "service_code_reason": "Assistência técnica",
                "items": [], "evidence": {
                    "number": number, "provider": "EMPRESA TESTE LTDA",
                    "total": "100,00", "description": "Serviço",
                }, "notes": "",
            }]}
            return {"steps": [{
                "type": "model_output",
                "content": [{"type": "text", "text": json.dumps(payload)}],
            }]}

        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "test-key", "NFREADER_GEMINI_PAGE_DELAY": "0",
            "NFREADER_QUEUE_RETRY_DELAYS": "0", "NFREADER_QUEUE_MAX_ATTEMPTS": "0",
        }), patch.object(gemini_engine, "_request_page", side_effect=request_page):
            result = gemini_engine.analyze_fiscal_pdf(
                self._pdf_with_pages(3),
                "fila.pdf",
                progress_callback=lambda _done, _total, message="": progress_messages.append(message),
            )

        self.assertEqual(call_order, [1, 2, 3, 2])
        self.assertEqual([document["pages"] for document in result], [[1, 2], [3]])
        self.assertTrue(any("fim da fila" in message for message in progress_messages))
        visible_progress = " ".join(progress_messages).upper()
        self.assertNotIn("GEMINI", visible_progress)
        self.assertNotIn(" IA ", f" {visible_progress} ")

    def test_unavailable_pdf_goes_behind_other_files(self):
        call_order = []
        first_file_failed = False
        progress_messages = []

        def request_page(_bytes, prompt, *_args, **_kwargs):
            nonlocal first_file_failed
            filename = prompt.split("Arquivo: ", 1)[1].split(".", 1)[0] + ".pdf"
            call_order.append(filename)
            if filename == "primeiro.pdf" and not first_file_failed:
                first_file_failed = True
                raise gemini_engine.RetryablePageError("indisponível")
            payload = {"documents": [{
                "pages": [1], "document_type": "NFSE", "confidence": 95,
                "is_continuation": False, "number": filename,
                "provider_name": "EMPRESA TESTE LTDA", "provider_cnpj": "",
                "customer_name": "CLIENTE", "total_value": "100,00",
                "service_description": "Serviço", "service_code": "750027",
                "service_code_reason": "Assistência técnica", "items": [],
                "evidence": {"number": filename, "provider": "EMPRESA", "total": "100,00", "description": "Serviço"},
                "notes": "",
            }]}
            return {"steps": [{
                "type": "model_output",
                "content": [{"type": "text", "text": json.dumps(payload)}],
            }]}

        files = [
            ("primeiro.pdf", self._pdf_with_pages(1)),
            ("segundo.pdf", self._pdf_with_pages(1)),
            ("terceiro.pdf", self._pdf_with_pages(1)),
        ]
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "test-key", "NFREADER_GEMINI_PAGE_DELAY": "0",
            "NFREADER_QUEUE_RETRY_DELAYS": "0", "NFREADER_QUEUE_MAX_ATTEMPTS": "0",
        }), patch.object(gemini_engine, "_request_page", side_effect=request_page):
            result = gemini_engine.analyze_fiscal_pdfs(
                files,
                progress_callback=lambda _done, _total, message="": progress_messages.append(message),
            )

        self.assertEqual(
            call_order,
            ["primeiro.pdf", "segundo.pdf", "terceiro.pdf", "primeiro.pdf"],
        )
        self.assertEqual([filename for filename, _bytes, _docs in result], [x[0] for x in files])
        self.assertTrue(any("arquivo movido para o fim da fila" in message for message in progress_messages))
        self.assertEqual(sum(len(documents) for _name, _bytes, documents in result), 3)

    def test_blank_calibration_code_uses_focused_second_stage(self):
        payload = {"documents": [{
            "pages": [1], "document_type": "NFSE", "confidence": 97,
            "is_continuation": False, "number": "845",
            "provider_name": "LABORATORIO METROLOGICO LTDA", "provider_cnpj": "",
            "customer_name": "CLIENTE", "total_value": "850,00",
            "service_description": "Calibração e aferição de instrumentos de medição",
            "service_code": "", "service_code_reason": "",
            "items": [], "evidence": {
                "number": "845", "provider": "LABORATORIO METROLOGICO LTDA",
                "total": "850,00", "description": "Calibração dos instrumentos",
            }, "notes": "",
        }]}
        response = {"steps": [{
            "type": "model_output",
            "content": [{"type": "text", "text": json.dumps(payload)}],
        }]}
        catalog = [
            {"codigo": "700186", "descricao": "SERVICO DE CALIBRACAO"},
            {"codigo": "750027", "descricao": "SERVICO ASSISTENCIA TECNICA"},
        ]
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "test-key", "NFREADER_GEMINI_PAGE_DELAY": "0",
        }), patch.object(
            gemini_engine, "_request_page", return_value=response
        ) as request_page, patch.object(
            gemini_engine, "_request_service_code",
            return_value=("700186", "A nota descreve calibração de instrumentos."),
        ) as classify:
            result = gemini_engine.analyze_fiscal_pdf(
                self._pdf_with_pages(1), "calibracao.pdf", service_catalog=catalog
            )

        self.assertEqual(result[0]["service_code"], "700186")
        self.assertIn("calibração", result[0]["service_code_reason"].lower())
        classify.assert_called_once()
        sent_schema = request_page.call_args.kwargs["response_schema"]
        code_schema = sent_schema["properties"]["documents"]["items"]["properties"]["service_code"]
        self.assertEqual(code_schema["enum"], ["", "700186", "750027"])

    def test_focused_service_request_uses_closed_catalog(self):
        classification = {"service_code": "700186", "reason": "Calibração de instrumentos."}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"steps": [{
                    "type": "model_output",
                    "content": [{"type": "text", "text": json.dumps(classification)}],
                }]}).encode("utf-8")

        document = {
            "provider_name": "LABORATORIO LTDA",
            "service_description": "Aferição e calibração de instrumentos",
            "evidence": {"description": "CALIBRACAO DOS INSTRUMENTOS"},
            "notes": "",
        }
        catalog_text = "- 700186: SERVICO DE CALIBRACAO\n- 750027: SERVICO ASSISTENCIA TECNICA"
        with patch.object(
            gemini_engine.urllib.request, "urlopen", return_value=FakeResponse()
        ) as urlopen:
            code, reason = gemini_engine._request_service_code(
                document, catalog_text, ["700186", "750027"],
                "key", "gemini-3.5-flash-lite", 10,
            )

        self.assertEqual(code, "700186")
        self.assertIn("Calibração", reason)
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            body["response_format"]["schema"]["properties"]["service_code"]["enum"],
            ["", "700186", "750027"],
        )
        self.assertIn("Aferição e calibração", body["input"][0]["text"])

    def test_service_classification_retry_does_not_reread_pdf(self):
        payload = {"documents": [{
            "pages": [1], "document_type": "NFSE", "confidence": 96,
            "is_continuation": False, "number": "900",
            "provider_name": "LABORATORIO LTDA", "provider_cnpj": "",
            "customer_name": "CLIENTE", "total_value": "100,00",
            "service_description": "Calibração de balança", "service_code": "",
            "service_code_reason": "", "items": [],
            "evidence": {"number": "900", "provider": "LABORATORIO", "total": "100,00", "description": "Calibração"},
            "notes": "",
        }]}
        response = {"steps": [{
            "type": "model_output",
            "content": [{"type": "text", "text": json.dumps(payload)}],
        }]}
        catalog = [{"codigo": "700186", "descricao": "SERVICO DE CALIBRACAO"}]
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "test-key", "NFREADER_GEMINI_PAGE_DELAY": "0",
            "NFREADER_QUEUE_RETRY_DELAYS": "0",
        }), patch.object(
            gemini_engine, "_request_page", return_value=response
        ) as request_page, patch.object(
            gemini_engine, "_request_service_code",
            side_effect=[
                gemini_engine.RetryablePageError("indisponível"),
                ("700186", "Calibração de balança."),
            ],
        ) as classify:
            result = gemini_engine.analyze_fiscal_pdf(
                self._pdf_with_pages(1), "calibracao.pdf", service_catalog=catalog
            )

        self.assertEqual(result[0]["service_code"], "700186")
        self.assertEqual(request_page.call_count, 1)
        self.assertEqual(classify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
