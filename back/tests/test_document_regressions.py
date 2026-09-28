import asyncio
from io import BytesIO
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook


BACK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACK_DIR))

import main  # noqa: E402
from document_segmentation import PageSignature, _page_classification, group_pages  # noqa: E402


def signature(page: int, text: str, number: str) -> PageSignature:
    return PageSignature(
        page=page,
        tipo="OUTRO",
        confianca_tipo=98.0,
        texto=text,
        cnpjs=["56073307000843"],
        chaves=[],
        numeros=[number],
        tokens_cabecalho=["LISTADEEMBARQUE"],
        linhas=text.splitlines(),
        palavras_rodape=[],
        visual_profile=[120] * 40,
    )


class DocumentRegressionTests(unittest.TestCase):
    def test_nfe_row_keeps_issuer_code_separate_from_internal_code(self):
        cadastro = {"rows": [{
            "codigo": "TOTVS-900", "descricao": "PECA INDUSTRIAL",
            "texto_match": "PECA INDUSTRIAL", "texto_match_extra": "", "ncm": "",
        }]}
        item = {
            "numero": "321", "empresa": "FORNECEDOR LTDA",
            "codigo_produto_emitente": "FAB-ABC/42",
            "descricao_nota": "PECA INDUSTRIAL", "quantidade": 2,
            "confianca_ocr": 98, "arquivo": "nota.pdf",
        }
        row = main.correlacionar([item], cadastro, "CC", "NF-E")[0]
        self.assertEqual(row["Código do Produto do Emitente"], "FAB-ABC/42")
        self.assertEqual(row["Código no Banco de Dados"], "TOTVS-900")
        self.assertNotEqual(
            row["Código do Produto do Emitente"], row["Código no Banco de Dados"]
        )

    def test_normal_reading_pipeline_carries_issuer_product_code_to_review(self):
        documents = [{
            "pages": [1], "document_type": "NFE", "confidence": 98,
            "number": "321", "provider_name": "FORNECEDOR LTDA",
            "items": [{
                "product_code": "FAB-ABC/42", "description": "PECA INDUSTRIAL",
                "quantity": 2, "ncm": "84818099",
            }], "evidence": {}, "notes": "",
        }]
        produtos = {"rows": [{
            "codigo": "TOTVS-900", "descricao": "PECA INDUSTRIAL",
            "texto_match": "PECA INDUSTRIAL", "texto_match_extra": "",
            "ncm": "84818099",
        }]}
        session = {
            "created_at": 0, "modo": "normal", "centro_custo": "CC",
            "documents": {}, "nfe_rows": [], "nfse_rows": [], "cte_rows": [],
            "outros_rows": [], "orcamento_rows": [],
        }
        with patch.object(
            main, "analyze_fiscal_pdfs", return_value=[("nota.pdf", b"%PDF", documents)]
        ), patch.object(main, "_subset_pdf", return_value=b"%PDF"):
            result = main._process_normal_with_gemini(
                [("nota.pdf", b"%PDF")], produtos, {"rows": []}, "CC",
                "session-product-code", session, None,
            )
        row = result["nfe_rows"][0]
        self.assertEqual(row["Código do Produto do Emitente"], "FAB-ABC/42")
        self.assertEqual(row["Código no Banco de Dados"], "TOTVS-900")

    def test_normal_reading_pipeline_preserves_fractional_quantities(self):
        documents = [{
            "pages": [1], "document_type": "NFE", "confidence": 98,
            "number": "321", "provider_name": "FORNECEDOR LTDA",
            "items": [
                {"product_code": "A", "description": "PECA", "quantity": "8.0000"},
                {"product_code": "B", "description": "PECA", "quantity": "0,0080"},
            ],
        }]
        session = {"created_at": 0, "modo": "normal", "centro_custo": "CC", "documents": {},
                   "nfe_rows": [], "nfse_rows": [], "cte_rows": [], "outros_rows": [], "orcamento_rows": []}
        with patch.object(main, "analyze_fiscal_pdfs", return_value=[("nota.pdf", b"%PDF", documents)]), \
             patch.object(main, "_subset_pdf", return_value=b"%PDF"):
            result = main._process_normal_with_gemini(
                [("nota.pdf", b"%PDF")], {"rows": []}, {"rows": []}, "CC",
                "session-quantities", session, None,
            )
        self.assertEqual([row["Quantidade"] for row in result["nfe_rows"]], ["8.0000", "0,0080"])

    def test_native_nfe_parser_extracts_issuer_product_code(self):
        lines = [
            "DADOS DOS PRODUTOS / SERVIÇOS",
            "ABC-001/XP PECA INDUSTRIAL 8481.80.99 060 5102 UN 2,000 10,00 20,00",
            "DADOS ADICIONAIS",
        ]
        items = main.extract_nfe_native(lines)
        self.assertEqual(items[0]["codigo_fornecedor"], "ABC-001/XP")

    def test_native_nfe_parser_keeps_fraction_and_trailing_zeroes(self):
        lines = [
            "DADOS DOS PRODUTOS / SERVIÇOS",
            "ABC-001 PECA INDUSTRIAL 84818099 060 5102 UN 8.0000 10,00 80,00",
            "ABC-002 PRODUTO FRACIONADO 84818099 060 5102 KG 0,0080 10,00 0,08",
            "DADOS ADICIONAIS",
        ]
        self.assertEqual([item["quantidade"] for item in main.extract_nfe_native(lines)], ["8.0000", "0,0080"])

    def test_nfe_excel_keeps_quantity_as_text(self):
        session_id = "quantity-export-test"
        main.REVISION_SESSIONS[session_id] = {"created_at": main.time.time()}
        async def export_bytes():
            response = await main.exportar(main.ExportRequest(
                session_id=session_id,
                nfe_rows=[{"Quantidade": "8.0000"}, {"Quantidade": "0,0080"}],
            ))
            return b"".join([chunk async for chunk in response.body_iterator])
        try:
            workbook = load_workbook(BytesIO(asyncio.run(export_bytes())))
            cells = workbook["Produtos (NF-e)"]["A2":"A3"]
            self.assertEqual([row[0].value for row in cells], ["8.0000", "0,0080"])
            self.assertTrue(all(row[0].data_type == "s" and row[0].number_format == "@" for row in cells))
        finally:
            main.REVISION_SESSIONS.pop(session_id, None)

    def test_review_keeps_file_and_notes_for_other_documents(self):
        session = {
            "modo": "normal", "documents": {}, "nfe_rows": [], "nfse_rows": [],
            "cte_rows": [], "orcamento_rows": [],
            "outros_rows": [{"Arquivo": "boleto.pdf", "Observações": "Revisar"}],
        }
        row = main._build_review_payload("session-review", session)["outros_rows"][0]
        self.assertEqual(row["Arquivo"], "boleto.pdf")
        self.assertEqual(row["Observações"], "Revisar")

    def test_calibration_nfse_fills_totvs_service_code(self):
        cadastro = {"rows": [
            {"codigo": "700186", "descricao": "SERVICO DE CALIBRACAO"},
            {"codigo": "750027", "descricao": "SERVICO ASSISTENCIA TECNICA"},
        ]}
        item = {
            "numero": "845", "empresa": "LABORATORIO METROLOGICO LTDA",
            "descricao_nota": "Calibração e aferição de instrumentos de medição",
            "quantidade": 1, "valor_total": "850,00", "confianca_ocr": 97,
            "codigo_ia": "700186",
            "motivo_codigo_ia": "A nota descreve calibração de instrumentos.",
        }
        row = main.correlacionar([item], cadastro, "CC", "NFS-E")[0]
        self.assertEqual(row["Código no Banco de Dados"], "700186")
        self.assertEqual(row["Descrição no Banco de Dados"], "SERVICO DE CALIBRACAO")

    def test_nfse_uses_ai_catalog_code_without_keyword_matching(self):
        cadastro = {"rows": [
            {"codigo": "750685", "descricao": "SERVICO ASSISTENCIA TECNICA ( HORA HOMEM )"},
            {"codigo": "715237", "descricao": "LOCACAO DE ANDAIMES"},
        ]}
        item = {
            "numero": "123", "empresa": "EMPRESA TESTE LTDA",
            "descricao_nota": "Atendimento técnico industrial especializado",
            "quantidade": 1, "valor_total": "100,00", "confianca_ocr": 94,
            "codigo_ia": "750685", "motivo_codigo_ia": "Serviço cobrado por hora-homem.",
        }
        row = main.correlacionar([item], cadastro, "CC", "NFS-E")[0]
        self.assertEqual(row["Código no Banco de Dados"], "750685")
        self.assertIn("Classificação automática", row["Observações"])

    def test_nfse_rejects_ai_code_outside_catalog(self):
        cadastro = {"rows": [{"codigo": "715237", "descricao": "LOCACAO DE ANDAIMES"}]}
        item = {
            "numero": "123", "empresa": "EMPRESA TESTE LTDA",
            "descricao_nota": "Serviço qualquer", "quantidade": 1,
            "valor_total": "100,00", "confianca_ocr": 94,
            "codigo_ia": "999999", "motivo_codigo_ia": "Código inventado.",
        }
        row = main.correlacionar([item], cadastro, "CC", "NFS-E")[0]
        self.assertEqual(row["Código no Banco de Dados"], "")
        self.assertIn("código inexistente", row["Observações"])

    def test_andaimes_elimar_always_forces_fixed_service_code(self):
        documents = [{
            "pages": [1], "document_type": "NFSE", "confidence": 97,
            "number": "456", "provider_name": "ANDAIMES ELIMAR LTDA",
            "total_value": "1.000,00", "service_description": "Serviço diverso",
            "service_code": "750685", "service_code_reason": "Escolha original da IA",
            "items": [], "evidence": {}, "notes": "",
        }]
        servicos = {"rows": [
            {"codigo": "715237", "descricao": "LOCACAO DE ANDAIMES"},
            {"codigo": "750685", "descricao": "SERVICO ASSISTENCIA TECNICA ( HORA HOMEM )"},
        ]}
        session = {
            "created_at": 0, "modo": "normal", "centro_custo": "CC",
            "documents": {}, "nfe_rows": [], "nfse_rows": [], "cte_rows": [],
            "outros_rows": [], "orcamento_rows": [],
        }
        with patch.object(
            main, "analyze_fiscal_pdfs",
            return_value=[("nota.pdf", b"%PDF", documents)],
        ), patch.object(
            main, "_subset_pdf", return_value=b"%PDF"
        ):
            result = main._process_normal_with_gemini(
                [("nota.pdf", b"%PDF")], {"rows": []}, servicos, "CC",
                "session-elimar", session, None,
            )
        row = result["nfse_rows"][0]
        self.assertEqual(row["Código no Banco de Dados"], "715237")
        self.assertIn("Regra fixa", row["Observações"])

    def test_nfse_ocr_variants_are_not_mistaken_for_nfe(self):
        sorocaba = """
        PREFEITURA DE SOROCABA SECRETARIA DA FAZENDA
        Nota Fiscal de Servigos Eletrénica - NFS-o
        DADOS DA NFS EMITENTE DA NFS TOMADOR DO SERVICO
        DETALHAMENTO DO SERVIGO CALCULO DO ISSQN
        """
        cascavel = """
        DANFS0 v2.0 Documento Ausillar da NFS-0
        PRESTADOR/FORNECEDOR TOMADOR/ADQUIRENTE
        SERVICO PRESTADO TRIBUTAGAO MUNICIPAL ISSQN
        """
        self.assertEqual(_page_classification(sorocaba)[0], "NFSE")
        self.assertEqual(_page_classification(cascavel)[0], "NFSE")

    def test_two_complete_shipping_lists_are_separate_documents(self):
        pages = [
            signature(20, "TETRA PAK\nLISTA DE EMBARQUE\n225059460", "225059460"),
            signature(21, "TETRA PAK\nLISTA DE EMBARQUE\n225059461", "225059461"),
        ]
        groups = group_pages(pages)
        self.assertEqual([[page.page for page in group] for group in groups], [[20], [21]])

    def test_unicode_page_range_can_be_served_inline(self):
        session_id = "unicode-header-session"
        document_id = "doc-20-21"
        main.REVISION_SESSIONS[session_id] = {
            "documents": {
                document_id: {
                    "filename": "NF para gerar SA-SC (1).pdf (páginas 20–21)",
                    "bytes": b"%PDF-1.4\n%%EOF",
                }
            }
        }
        response = asyncio.run(main.documento(session_id, document_id))
        header = response.headers["content-disposition"]
        header.encode("latin-1")
        self.assertIn("paginas 20-21", header)

    def test_sorocaba_scanned_nfse_extracts_number_company_and_net_total(self):
        lines = [
            "DADOS DA NFS-e",
            "Competência da NFS-e Número / Série",
            "09/2026",
            "57684 / U",
            "Número / Série do RPS",
            "57682 / NF",
            "EMITENTE DA NFS-e",
            "Nome/Razão Social",
            "VERTIV TECNOLOGIA DO BRASIL LTDA.",
            "TOMADOR DO SERVIÇO",
            "DETALHAMENTO DO SERVIÇO",
            "Serviço: manutenção preventiva industrial",
            "Município da Incidência do ISSQN",
            "VALOR TOTAL DA NOTA",
            "Base Cálculo ISSQN (R$)",
            "Retenções (R$)",
            "Descontos (R$)",
            "Valor Líquido (R$)",
            "2.863,78",
            "133,16",
            "0,00",
            "2.730,62",
            "INFORMAÇÕES COMPLEMENTARES",
        ]
        doc = main.extract_nfse(lines)
        self.assertEqual(doc["numero"], "57684")
        self.assertEqual(doc["empresa"], "VERTIV TECNOLOGIA DO BRASIL LTDA")
        self.assertEqual(doc["valor_total"], "2.730,62")
        self.assertEqual(doc["descricao"], "Serviço: manutenção preventiva industrial")

    def test_municipal_nfse_fields_are_extracted_from_provider_block(self):
        lines = [
            "PREFEITURA MUNICIPAL",
            "Número da Nota Fiscal: 00093 Emissão: 10/08/2026",
            "DADOS DO PRESTADOR DE SERVIÇOS",
            "Razão Social",
            "REFRIGERACAO INDUSTRIAL PARANA LTDA contato@empresa.com.br",
            "TRAVESSA DAS FLORES, 10",
            "DADOS DO TOMADOR DE SERVIÇOS",
            "CLIENTE ERRADO LTDA",
            "DETALHAMENTO DO SERVIÇO",
            "Assistência técnica e manutenção em refrigeração industrial",
            "CÁLCULO DO ISSQN",
            "Valor dos Serviços R$ 2.834,00",
        ]
        doc = main.extract_nfse(lines)
        self.assertEqual(doc["numero"], "93")
        self.assertEqual(doc["empresa"], "REFRIGERACAO INDUSTRIAL PARANA LTDA")
        self.assertIn("Assistência técnica", doc["descricao"])
        self.assertNotIn("CLIENTE ERRADO", doc["empresa"])

    def test_company_filter_rejects_regime_and_address(self):
        self.assertEqual(main._candidato_empresa_nfse("Simples Nacional na Data de Competência Regime"), "")
        self.assertEqual(main._candidato_empresa_nfse("TRAVESSA DAS FLORES, 10"), "")
        self.assertEqual(
            main._candidato_empresa_nfse("AUTO ELETRICA ROMITO LTDA contato@aeromito.com.br"),
            "AUTO ELETRICA ROMITO LTDA",
        )
        self.assertEqual(main._candidato_empresa_nfse("chave de acesso no portal nacional da NFS-e NFS-e MEI"),"")
        self.assertEqual(main._candidato_empresa_nfse("(14) 3301-1457 BCV INSTALACOES EIRELI"),"BCV INSTALACOES EIRELI")

    def test_national_nfse_recovers_number_from_access_key(self):
        lines = [
            "DANFSe v1.0",
            "Chave de Acesso da NFS-e",
            "41169501207151208000150000000003292426090572876320",
            "Número da NFS-e",
            "Emitente da NFS-e",
            "Nome / Nome Empresarial",
            "SABIA ECOLOGICO TRANSPORTE DE LIXO EIRELI",
        ]
        self.assertEqual(main.extract_nfse(lines)["numero"], "32924")

    def test_compact_municipal_service_row_extracts_description_and_total(self):
        lines = [
            "Nomero da Nota Fiscal",
            "1363",
            "Nome/Razio Sociat: MOTORES ELETRICOS TUTIA LTDA",
            "DADOS DO TOMADOR",
            "SERVICO",
            "SERVICOS E REBOBINAMENTOS DE MOTORES ELETRICOS + ACESSORIOS Sim 1,00 3902 3.902,00",
            "Valor Tributável:",
        ]
        doc=main.extract_nfse(lines)
        self.assertEqual(doc["numero"], "1363")
        self.assertEqual(doc["empresa"], "MOTORES ELETRICOS TUTIA LTDA")
        self.assertEqual(doc["descricao"], "SERVICOS E REBOBINAMENTOS DE MOTORES ELETRICOS + ACESSORIOS")
        self.assertEqual(doc["valor_total"], "3.902,00")

    def test_nfse_never_uses_tomador_as_provider(self):
        lines = [
            "Emitente da NFS-e",
            "Prestador do Serviço",
            "Nome / Nome Empresarial",
            "SABIA ECOLOGICO TRANSPORTE DE LIXO EIRELI",
            "TOMADOR DO SERVIÇO",
            "Nome / Nome Empresarial",
            "ZD ALIMENTOS S.A",
            "SERVIÇO PRESTADO",
            "DESCRIÇÃO SERVIÇOS VALOR TOTAL ALIQ. VALOR IMPOST RETIDO",
            "TRANSPORTE E DESTINACAO FINAL DOS RESIDUOS ORGANICOS R$ 1.425,00 3,00 R$ 42,75 Sim",
            "COLETA E DESTINACAO FINAL DE RESIDUOS SOLIDOS R$ 2.230,00 3,00 R$ 66,90 Sim",
            "TRIBUTAÇÃO MUNICIPAL",
        ]
        doc=main.extract_nfse(lines)
        self.assertEqual(doc["empresa"], "SABIA ECOLOGICO TRANSPORTE DE LIXO EIRELI")
        self.assertNotIn("ZD ALIMENTOS",doc["empresa"])
        self.assertIn("TRANSPORTE E DESTINACAO",doc["descricao"])
        self.assertNotIn("VALOR IMPOST",doc["descricao"])
        self.assertNotIn("RETIDO",doc["descricao"])

    def test_provider_name_split_across_ocr_lines_is_joined(self):
        lines = [
            "PRESTADOR / FORNECEDOR",
            "Nome / Nome empresarial",
            "CNPJ / CPF / NIF",
            "SHK SERVICOS E INSTALACOES",
            "65.179.432/0001-57",
            "130240",
            "INDUSTRIAIS LTDA",
            "TOMADOR DO SERVICO",
        ]
        self.assertEqual(
            main.extract_nfse(lines)["empresa"],
            "SHK SERVICOS E INSTALACOES INDUSTRIAIS LTDA",
        )


if __name__ == "__main__":
    unittest.main()
