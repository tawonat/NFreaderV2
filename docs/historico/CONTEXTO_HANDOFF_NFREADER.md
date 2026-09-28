# CONTEXTO DE HANDOFF — NFreader (projeto atual)

> **Use este arquivo como prompt de contexto em uma nova conversa.**
> O objetivo é preservar o histórico técnico e funcional do projeto para continuar o desenvolvimento sem voltar etapas desnecessariamente.

---

# 1. O QUE É O PROJETO

O **NFreader** é uma ferramenta local desenvolvida para uso do **PCM de uma fábrica da Leite Hércules / grupo ZD Alimentos**.

Objetivo principal:

- receber PDFs e ZIPs com documentos fiscais/administrativos;
- identificar cada documento;
- ler os documentos com OCR/localmente;
- extrair dados relevantes;
- correlacionar produtos e serviços com cadastros fixos do TOTVS;
- permitir conferência/correção humana antes da exportação;
- gerar um Excel padronizado.

A aplicação é local. Não foi desenhada como um sistema multiusuário complexo. O foco é **precisão, conferência e produtividade do PCM**.

A grande prioridade do projeto é: **máxima precisão possível na leitura**, mesmo que isso aumente o tempo de processamento.

---

# 2. TECNOLOGIA / ESTRUTURA ATUAL

## Backend

- Python
- FastAPI
- Uvicorn
- pdfplumber
- pypdf / PdfReader / PdfWriter
- pypdfium2
- Pillow
- pytesseract
- OpenCV quando disponível
- RapidFuzz
- pandas
- openpyxl

O backend atualmente está dividido em vários módulos para não concentrar tudo em `main.py`.

Arquivos principais atuais:

```text
back/
├── main.py
├── ocr_engine.py
├── document_segmentation.py
├── page_worker.py
├── segment_worker.py
├── requirements.txt
├── start_backend.bat
├── MAPA_SEMANTICO_SERVICOS.md
├── README_OCR.md
└── cadastro/
    ├── produtos_totvs.xlsx
    └── servicos_totvs.xlsx
```

## Frontend

- Next.js
- React
- TypeScript
- CSS próprio

Principais arquivos:

```text
front/
├── package.json
├── package-lock.json
├── src/app/page.tsx
├── src/app/globals.css
├── src/app/layout.tsx
└── public/hercules-logo.png
```

---

# 3. CADASTROS TOTVS

Os cadastros ficam incorporados ao projeto e não precisam ser enviados pelo usuário toda vez.

Arquivos:

```text
back/cadastro/produtos_totvs.xlsx
back/cadastro/servicos_totvs.xlsx
```

O cadastro de produtos tem dezenas de milhares de registros (aprox. 37 mil no acervo usado no desenvolvimento).

O cadastro de serviços possui poucos registros e é usado para correlação semântica de NFS-e/serviços.

No projeto já existe um mapa semântico em:

```text
back/MAPA_SEMANTICO_SERVICOS.md
```

Alguns códigos de serviços importantes já tratados:

```text
700186  SERVICO DE CALIBRACAO
715237  LOCACAO DE ANDAIMES
717703  LOCACAO DE MARTELETE ROMPEDOR
730009  SERVICO LOCACAO EQUIPAMENTOS CONSTRUCAO CIVIL
730170  SERVICO DE CONCRETAGEM
750027  SERVICO ASSISTENCIA TECNICA
750685  SERVICO ASSISTENCIA TECNICA ( HORA HOMEM )
760000  SERVICO HOSPEDAGEM
771206  SERVICO DESTINACAO RESIDUO
780007  SERVICO TRANSPORTE
780117  LOCACAO DE PLATAFORMA ELEVATORIA
830132  LOCACAO DE CACAMBAS LIXO/ENTULHO
900076  SERVICO DE LOCACAO DATADORA
900136  LOCACAO DE GERADOR
```

Durante os testes também foi observado que existe no cadastro um registro de refrigeração, `650140`, e isso deve ser considerado na interpretação dos serviços quando aparecer no cadastro real.

---

# 4. FUNCIONALIDADES JÁ IMPLEMENTADAS NA V2

## 4.1 Processamento normal

Fluxo conceitual:

```text
PDF/ZIP
→ análise
→ resultado provisório
→ tela de revisão
→ usuário confere/corrige
→ exportação Excel
```

O Excel não é gerado imediatamente após a análise.

---

## 4.2 Tela de revisão

A tela de revisão é parte central da V2.

Ela permite:

- ver o documento ao lado do resultado;
- fechar o painel do PDF;
- reabrir o PDF quando necessário;
- editar manualmente os campos;
- visualizar candidatos de correlação;
- aprovar/corrigir antes de exportar.

O PDF **não abre automaticamente o Banco TOTVS** quando o usuário clica nos campos.

Os campos são editáveis normalmente.

O Banco TOTVS só deve abrir quando o usuário clicar explicitamente no botão:

```text
Pesquisar no banco TOTVS
```

---

## 4.3 Confiança por campo

A confiança é mostrada **somente na tela de revisão**.

Ela vale para:

- NF-e
- NFS-e
- Orçamento
- e deve continuar valendo para CT-e quando essa categoria for integrada à revisão.

Exemplo conceitual:

```text
Descrição na Nota        96%
Quantidade               92%
Código TOTVS             88%
Descrição TOTVS         90%
```

A confiança **não deve ser exportada para o Excel**.

A interface usa faixas como Alta/Revisar/Baixa/Muito baixa.

---

## 4.4 Banco TOTVS — consulta manual

Existe uma tela própria para pesquisa manual do cadastro.

A pesquisa pode ser feita:

- pela tela inicial;
- pela revisão de um item.

Pesquisa por:

- código;
- palavra-chave;
- trecho da descrição.

O usuário pode selecionar um resultado e aplicar ao item em revisão.

---

## 4.5 Preenchimento automático ao digitar código

Quando o usuário editar manualmente o campo:

```text
Código no Banco de Dados
```

e sair do campo, o sistema consulta o cadastro TOTVS.

Se o código existir:

```text
Código digitado
→ encontra cadastro
→ preenche automaticamente a Descrição no Banco de Dados
```

Se não existir, não deve inventar descrição.

---

## 4.6 Exportação Excel

A exportação não tem mais aba `Resumo`.

Estrutura prevista:

```text
Produtos (NF-e)
Serviços (NFS-e)
Outros
Orçamentos
```

A confiança fica fora do Excel.

O campo `Arquivo` deve permanecer no final das tabelas relevantes.

---

# 5. NF-e — REGRAS ATUAIS

NF-e é correlacionada contra o cadastro de produtos do TOTVS.

O código do fornecedor não é necessariamente o código interno do TOTVS. A correlação deve ser feita principalmente por descrição/conjunto de características.

A intenção do sistema é:

- encontrar o melhor candidato;
- mostrar alternativas viáveis;
- permitir revisão humana quando necessário.

Em caso de dúvida, não deve simplesmente apagar tudo ou inventar certeza.

Foi estabelecida a ideia de mostrar alternativas como:

```text
Outras peças viáveis: X, Y
```

mesmo quando a confiança estiver abaixo de 70%, desde que realmente sejam candidatos plausíveis.

Há atenção especial a:

- números/medidas;
- dimensões;
- descrição;
- unidade;
- quantidade;
- posição/layout de tabela.

Não depender de NCM puro, porque OCR pode misturar NCM/CST e outros números.

---

# 6. NFS-e — REGRAS ATUAIS E PROBLEMAS CONHECIDOS

A NFS-e é **muito mais variável** que NF-e.

Não assumir um único layout.

Já foram encontrados layouts de vários municípios, incluindo exemplos de:

- Sorocaba;
- Tupã;
- Marechal Cândido Rondon;
- Nova Esperança;
- padrão nacional;
- layouts genéricos/customizados.

A descrição correta do serviço normalmente está em uma seção específica como:

```text
SERVIÇO PRESTADO
DETALHAMENTO DO SERVIÇO
DISCRIMINAÇÃO DO SERVIÇO
DESCRIÇÃO SERVIÇOS
Serviço:
```

Não capturar automaticamente texto próximo de:

- Faturas/Duplicatas;
- Vencimento;
- Ordem de Venda;
- Contrato;
- Dados para pagamento;
- endereço;
- telefone;
- tributação.

Exemplo já observado em teste:

Descrição correta:

```text
LIMPEZA. REVISÃO. CARGA E RECARGA. CONSERTO,
RESTAURAÇÃO. BLINDAGEM
```

mas o parser antigo pegou textos de cobrança/contrato ao invés da descrição do serviço.

Outro exemplo:

```text
lubrificação, limpeza, lustração, revisão, carga e recarga,
conserto, restauração, blindagem, manutenção e conservação...
```

A descrição deve refletir o serviço prestado de forma completa, não apenas um rótulo genérico.

---

# 7. PROBLEMA ANTERIOR DE CORRELAÇÃO DE NFS-e

Em testes, muitas NFS-e acabavam incorretamente como:

```text
750027 — SERVICO ASSISTENCIA TECNICA
```

mesmo quando eram outras famílias.

Por isso foi criada uma interpretação semântica mais específica.

Exemplos desejados:

```text
GERADOR
→ 900136

CAÇAMBA / CACAMBA
→ 830132

RESÍDUO / LIXO / DESTINAÇÃO
→ 771206

MARTELETE / ROMPEDOR
→ 717703

ANDAIME
→ 715237

PLATAFORMA ELEVATÓRIA
→ 780117

CALIBRAÇÃO
→ 700186

CONCRETAGEM
→ 730170

DATADORA / CODIFICADORA / MARCADOR DE LOTE
→ 900076

HORA-HOMEM / HOMEM HORA / MAO DE OBRA em contexto técnico
→ 750685

REFRIGERAÇÃO / AR CONDICIONADO (quando suportado pelo cadastro)
→ 650140
```

`750027` deve entrar principalmente quando o contexto realmente indicar assistência/manutenção técnica.

Evitar classificações baseadas em uma única palavra genérica.

---

# 8. CT-e — REGRA DEFINIDA PELO USUÁRIO

CT-e = Conhecimento de Transporte Eletrônico / DACTE.

É um novo tipo documental e deve ser lido também.

Regra de negócio obrigatória:

```text
Detectou CT-e
→ tipo = CT-e
→ código TOTVS = 780007
```

`780007` já existe no cadastro de serviços como transporte.

O CT-e **não deve depender da correlação semântica dos 14 serviços**. O código é fixo.

Também não deve ser automaticamente jogado para `Outros` na versão final.

O código atual já consegue reconhecer sinais de CT-e/DACTE, mas a integração final da categoria CT-e na revisão/exportação ainda precisa ser concluída.

---

# 9. ORÇAMENTO / DEEP READING

Existe um modo especial chamado **Deep Reading**.

O usuário ativa antes de processar um orçamento.

Objetivo: **priorizar precisão em vez de velocidade**.

O Deep Reading pode usar várias leituras internas e gastar mais tempo/processamento.

Para orçamentos, o sistema deve tratar o documento como uma tabela de itens/peças, não como um bloco de texto genérico.

O resultado precisa identificar cada peça individualmente e correlacionar cada uma com o TOTVS.

Campos relevantes para revisão:

```text
Nome no orçamento
Código no banco de dados
Descrição no orçamento
Descrição no banco de dados
Quantidade
Preço unitário
Total da peça
Total do pedido
```

O PDF de exemplo `Idercio.PDF` mostrou uma tabela com seis itens. Os dados de item esperados foram semelhantes a:

```text
TBI0109005  TUBO AISI 304 19,05 X 1,50 X 6000 A554 PE   30,00   27,0700   812,10
TBI0113030  TUBO AISI 304 40 X 40 X 2,00 X 6000 A554 PE    6,00   98,3000   589,80
CVI0109005  CURVA RL90 304 19,05 X 1,50 PE                 8,00   14,7000   117,60
VTI0109007  VALVULA ESFERA TRIPARTIDA 304 33,40             2,00  161,7000   323,40
REI0109042  REDUCAO 304 25,40 X 19,05 PE                    4,00   35,2800   141,12
UNI0110007  UNIAO DE SOLDA 304 SMS 25,40                    6,00   60,7600   364,56
```

Total geral observado:

```text
TOTAL DO PEDIDO = 2.424,72
```

O Deep Reading anterior havia capturado lixo como endereço/telefone porque lia blocos genéricos. A abordagem correta é reconhecer a estrutura da tabela e excluir cabeçalho/rodapé.

A versão atual já possui a infraestrutura do Deep Reading, mas essa parte ainda precisa ser validada em profundidade depois de consolidar o motor documental.

---

# 10. PDFs GRANDES / VÁRIOS DOCUMENTOS NO MESMO PDF

Esse foi o maior problema estrutural descoberto mais recentemente.

Antes o sistema assumia implicitamente:

```text
1 PDF = 1 documento
```

Isso está errado.

Um PDF pode ter:

```text
20–30 páginas
```

e dentro dele podem existir:

- NF-e;
- NFS-e;
- CT-e;
- boleto;
- nota de débito;
- lista de embarque;
- fatura;
- outros documentos.

Também pode haver um documento com várias páginas, sendo algumas páginas continuações.

A arquitetura foi alterada para:

```text
1 PDF
→ páginas individuais
→ análise de cada página
→ classificação
→ assinatura documental
→ agrupamento de páginas
→ documentos independentes
→ parser específico de cada documento
```

O arquivo de exemplo de 22 páginas foi usado para testar isso.

---

# 11. DOCUMENT SEGMENTATION

Foi criada a camada:

```text
back/document_segmentation.py
```

Ela deve ser entendida como a camada que vem **antes do parser fiscal**.

Responsabilidades:

- analisar página por página;
- classificar tipo provável;
- extrair identificadores;
- calcular assinatura da página;
- detectar começo/continuação;
- agrupar páginas pertencentes ao mesmo documento;
- produzir grupos para o parser fiscal.

Estrutura conceitual:

```text
PDF
 ↓
PAGE ANALYZER
 ↓
DOCUMENT GROUPER
 ↓
┌────────┬────────┬────────┐
NF-e    NFS-e     CT-e    Outros
└────────┴────────┴────────┘
 ↓
parser específico
```

Sinais utilizados incluem:

- chave de acesso;
- número;
- série;
- CNPJ emitente/destinatário;
- data;
- tipo documental;
- empresa;
- marcadores textuais;
- similaridade de layout;
- cabeçalho/rodapé;
- continuidade dos itens.

Não usar apenas proximidade de página para agrupar.

Duas páginas com o mesmo layout podem ser documentos diferentes.

---

# 12. OCR DE PDFs LONGOS — PROBLEMA E MITIGAÇÃO

Foi descoberto que o Tesseract pode ficar instável/travar quando muitas páginas são processadas no mesmo processo.

A mitigação adotada é:

```text
Página 1 → worker isolado → encerra
Página 2 → worker isolado → encerra
Página 3 → worker isolado → encerra
...
```

Com concorrência limitada e timeout por página.

Existem os arquivos:

```text
back/page_worker.py
back/segment_worker.py
```

Variáveis opcionais:

```text
NFREADER_SEGMENT_WORKERS
NFREADER_SEGMENT_PAGE_TIMEOUT
NFREADER_OCR_WORKERS
NFREADER_DEEP_OCR_TIMEOUT
```

A segmentação já foi testada com o PDF de 22 páginas e conseguiu percorrer/classificar as páginas sem travar como acontecia anteriormente.

A regra importante é: **uma página problemática não pode derrubar o processamento do PDF inteiro**.

Também foi observado que documentos classificados como `Outros` não devem passar novamente pelo OCR fiscal pesado desnecessariamente.

---

# 13. CASO DE TESTE — PDF DE 22 PÁGINAS

Foi analisado um PDF chamado:

```text
NF para gerar SA-SC (1).pdf
```

Ele possui 22 páginas e mistura documentos.

Uma classificação obtida durante os testes foi aproximadamente:

```text
P1  NFSE
P2  NFSE
P3  NFSE
P4  NFSE
P5  NFSE
P6  NFE
P7  NFSE
P8  NFE
P9  NFE
P10 NFE
P11 NFE
P12 NFE
P13 NFE
P14 NFSE
P15 NFSE
P16 NFSE
P17 NFSE
P18 NFSE
P19 OUTRO / Nota de Débito
P20 OUTRO / Lista de Embarque
P21 OUTRO / continuação/Lista de Embarque
P22 NFSE
```

O agrupamento final ainda precisava de validação mais ampla com todo o corpus.

---

# 14. PROBLEMAS ENCONTRADOS NO TESTE DO PDF LONGO

O usuário testou a versão anterior e relatou:

- páginas 4, 7 e 18 não foram lidas corretamente;
- páginas 20–21 tiveram `Internal Server Error`;
- NFS-e tiveram descrições erradas, frequentemente puxando faturas, parcelas, contratos, endereço, telefone etc.;
- muitas NFS-e caíam sempre em `SERVICO ASSISTENCIA TECNICA`.

Exemplos de correção esperada:

Página 1: o serviço real era:

```text
LIMPEZA. REVISÃO. CARGA E RECARGA.
CONSERTO, RESTAURAÇÃO. BLINDAGEM
```

mas o parser antigo pegava texto de faturamento/contrato.

Página 17: o serviço real era longo e começava com algo como:

```text
lubrificação, limpeza, lustração, revisão, carga e recarga,
conserto, restauração, blindagem, manutenção e conservação...
```

mas o parser antigo pegava uma frase relacionada a PMOC/vencimento.

Esses casos devem virar **testes de regressão**.

---

# 15. CORPUS DE TESTES EXISTENTE

O usuário enviou vários ZIPs por cidade/unidade para criar um corpus diversificado.

Os conjuntos enviados incluem aproximadamente:

```text
NFS_UNIDADE_DE_MARECHAL_CANDIDO_RONDON.zip
ENC__NFS_UNIDADE_SÃO_JOÃO_DO_CAIUA.zip
ENC__NFS_UNIDADE_HERCULANDIA.zip
NFS_HERCULANDIA.zip
```

Também há documentos de teste individuais e diversos exemplos adicionais.

Durante análise anterior foi indicado um corpus de dezenas de PDFs e mais de 100 páginas com layouts diferentes.

Os materiais devem ser usados como **bateria de testes e regressão**, e não como regra para assumir um único layout.

A intenção do usuário é continuar enviando mais modelos de NF-e/NFS-e/CT-e para ampliar a cobertura.

---

# 16. FILOSOFIA DE CLASSIFICAÇÃO

O sistema precisa assumir que existirão:

- layouts novos;
- páginas escaneadas;
- OCR imperfeito;
- páginas rotacionadas;
- documentos misturados;
- documentos errados;
- documentos incompletos;
- documentos não fiscais;
- municípios diferentes;
- documentos com várias páginas;
- layouts ainda não conhecidos.

Portanto, **não criar lógica dependente de um único layout**.

A abordagem deve ser:

```text
regras fortes quando houver evidência forte
+
heurísticas/layout quando apropriado
+
OCR/fallback genérico
+
revisão humana quando realmente incerto
```

Nunca forçar um documento desconhecido para NF-e/NFS-e.

---

# 17. DESIGN / UX ATUAL

A identidade visual foi direcionada totalmente para a **Leite Hércules**, não para uma mistura ZDA + Hércules.

O usuário forneceu uma imagem da logo da Hércules, e ela foi incorporada ao frontend como:

```text
front/public/hercules-logo.png
```

Estilo desejado:

- corporativo + moderno;
- simples;
- enxuto;
- pouco excesso de cards;
- azul inspirado na marca Hércules;
- interface clara.

Na revisão:

- PDF pode ser fechado;
- PDF pode ser reaberto;
- resultados ocupam mais espaço quando o PDF está fechado;
- campos continuam editáveis;
- Banco TOTVS só abre via botão explícito.

---

# 18. PROGRESSO DE PROCESSAMENTO

Existe uma barra circular de progresso durante o processamento.

Ela mostra o percentual dos documentos/processamento já concluído.

No frontend existem estados como:

```text
progresso
progressoStatus
```

e polling do endpoint:

```text
/progresso/{job_id}
```

---

# 19. ENDPOINTS IMPORTANTES DO BACKEND

Atualmente existem endpoints relacionados a:

- iniciar análise;
- consultar progresso;
- pesquisar cadastro TOTVS;
- exportar Excel;
- e outros auxiliares.

A interface usa:

```text
http://localhost:8000
```

como API local.

---

# 20. EXECUÇÃO LOCAL NO WINDOWS

## Backend — primeira vez

```powershell
cd "C:\caminho\NFreader V2\back"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn main:app --reload
```

Se o PowerShell bloquear a ativação:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## Backend — próximas vezes

```powershell
cd "C:\caminho\NFreader V2\back"
.\.venv\Scripts\Activate.ps1
python -m uvicorn main:app --reload
```

## Frontend

A instalação de Node do usuário fica em:

```text
C:\Users\otavio.seidinger\Desktop\NodeJS
```

Foi necessário usar `npm.cmd` em vez de `npm` em PowerShell.

Comando:

```powershell
$env:Path += ";C:\Users\otavio.seidinger\Desktop\NodeJS"
cd "C:\caminho\NFreader V2\front"
& "C:\Users\otavio.seidinger\Desktop\NodeJS\npm.cmd" install
& "C:\Users\otavio.seidinger\Desktop\NodeJS\npm.cmd" run dev
```

---

# 21. ESTADO EXATO DO ZIP DE HANDOFF

O ZIP anexado junto deste prompt corresponde ao estado de código usado como base ao final da rodada de desenvolvimento de **PDFs longos/multidocumentos**.

Arquivo-base original:

```text
NFreader_V2_multidoc_stable.zip
```

Neste estado existem:

- backend FastAPI;
- OCR modular;
- workers de página/segmentação;
- `document_segmentation.py`;
- cadastros TOTVS incorporados;
- frontend V2 com identidade Hércules;
- revisão;
- confiança por campo;
- Banco TOTVS;
- progresso;
- Deep Reading;
- exportação.

**Importante:** o código atual já possui reconhecimento de sinais de CT-e e o código `780007` no mapa de serviços, mas a integração final ainda não está concluída: no estado atual do `main.py`, CT-e ainda pode ser direcionado para `Outros` em determinados caminhos. A próxima conversa deve corrigir isso para virar uma categoria própria.

---

# 22. O QUE AINDA PRECISA SER FEITO

Prioridade sugerida:

## P1 — fechar robustez documental

- validar agrupamento de documentos no corpus inteiro;
- garantir que uma página problemática não derrube o PDF;
- garantir que documentos `Outros` não recebam OCR fiscal pesado desnecessário;
- validar CT-e ponta a ponta;
- validar casos rotacionados.

## P2 — fechar NFS-e multi-layout

- melhorar extração da descrição correta do serviço;
- remover interferência de cabeçalho/rodapé/faturamento;
- melhorar classificação semântica;
- criar regras de exclusão e de contexto;
- testar vários municípios;
- manter fallback genérico.

## P3 — melhorar NF-e multi-layout

- validar NF-e de diferentes fornecedores/formatos;
- melhorar layout/posição da tabela;
- melhorar quantidade e descrição;
- validar códigos candidatos.

## P4 — finalizar CT-e

- categoria própria no resultado/revisão;
- exportação correta;
- código fixo `780007`;
- leitura/identificação confiável de DACTE em diferentes orientações/layouts.

## P5 — finalizar Deep Reading

- identificar corretamente a tabela de itens;
- ignorar endereço/telefone/cabeçalho/rodapé;
- separar todas as peças;
- correlacionar cada peça com TOTVS;
- recuperar preço unitário e total da peça;
- recuperar total do pedido;
- revisão clara.

## P6 — ampliar bateria de testes

Usar todos os modelos que o usuário enviar de agora em diante.

Cada bug encontrado deve virar um caso de regressão para evitar quebrar documentos anteriores.

---

# 23. PRINCÍPIO IMPORTANTE PARA A PRÓXIMA CONVERSA

Não voltar a uma arquitetura de:

```text
1 PDF = 1 documento
```

A arquitetura correta é:

```text
PDF
→ páginas
→ classificação por página
→ agrupamento de documentos
→ parser específico
→ revisão
→ exportação
```

E o sistema deve tolerar mistura de documentos e layouts desconhecidos.

---

# 24. PEDIDO AO NOVO CHAT

Ao receber este contexto:

1. Leia o código do ZIP anexado antes de propor mudanças.
2. Não reescreva funcionalidades que já estão funcionando sem necessidade.
3. Preserve a V2 atual e faça alterações incrementais.
4. Sempre testar sintaxe do backend e do frontend antes de gerar ZIP.
5. Quando possível, rodar testes reais com o corpus disponível.
6. Não afirmar que uma versão está "pronta" sem validação real.
7. Usar os documentos reais como regressão.
8. Manter o foco em máxima precisão, não velocidade, especialmente em Deep Reading.
9. Se uma sessão de ferramentas estiver terminando, registrar o estado antes de parar.
10. Ao criar uma nova versão, entregar um ZIP completo e explicar claramente o que foi validado e o que ainda ficou pendente.

---

# 25. OBJETIVO FINAL

Quando terminado, o NFreader deve conseguir receber algo como:

```text
ZIP/PDF
  ├── NF-e
  ├── NF-e de outro layout
  ├── NFS-e municipal A
  ├── NFS-e municipal B
  ├── CT-e
  ├── boleto
  ├── documento desconhecido
  └── NF-e de 3 páginas
```

mesmo que tudo esteja misturado em um único arquivo PDF, e produzir uma revisão organizada com cada documento corretamente agrupado, classificado e processado.

A prioridade absoluta é **não perder informações nem inventar correspondências**. Quando houver dúvida real, o sistema deve facilitar a revisão humana.

---

# 26. ATUALIZAÇÃO — GEMINI, CÓDIGOS DE SERVIÇO E ESTABILIDADE

- A leitura normal continua página a página pela Interactions API.
- Modelo primário padrão: `gemini-3.5-flash-lite`.
- Em 429, 5xx ou timeout, o backend alterna automaticamente para os modelos de
  `GEMINI_FALLBACK_MODELS` e informa a tentativa atual na tela de progresso.
- O limite padrão por tentativa foi reduzido para 90 segundos, evitando que uma
  única fila lenta retenha silenciosamente o processamento por vários minutos.
- Para NFS-e, o catálogo real de serviços TOTVS é enviado ao Gemini. A IA escolhe
  um código e justifica a escolha; o backend aceita somente códigos existentes no
  cadastro. O mapa manual de palavras-chave não participa mais dessa decisão.
- Regra obrigatória: prestadores cujo nome contenha `ANDAIMES ELIMAR` recebem o
  código `715237` (`LOCACAO DE ANDAIMES`), independentemente da resposta da IA.
- Regressões adicionadas para seleção válida, rejeição de código inexistente,
  regra da ANDAIMES ELIMAR, fallback de modelo e união de páginas.

---

# 27. ATUALIZAÇÃO — FILA RESILIENTE DE PÁGINAS

- Cada página faz uma tentativa por passagem na fila.
- Falhas transitórias não encerram o pacote: a página recebe um cooldown e vai
  para o final, permitindo que as demais continuem.
- O modelo alternativo muda a cada nova passagem da página problemática.
- Páginas concluídas ficam preservadas em memória e não são processadas novamente.
- A fila tenta até concluir por padrão (`NFREADER_QUEUE_MAX_ATTEMPTS=0`).
- Esperas padrão por página: 30, 60, 120 e 180 segundos; depois permanece em 180.
- Ao final, os resultados são ordenados pelo número da página antes da detecção
  de continuações, preservando a ordem original mesmo quando a execução ocorreu
  fora de ordem.
- A interface, os status, erros e observações visíveis usam termos neutros como
  `leitura`, `classificação automática` e `serviço de leitura`; não citam IA nem
  o fornecedor do motor.

---

# 28. ATUALIZAÇÃO — FILA GLOBAL PARA ZIP COM VÁRIOS PDFS

- O bloqueio anterior ocorria porque cada PDF executava sua fila interna até o
  fim antes que o laço externo avançasse ao próximo arquivo.
- Agora os PDFs são processados em round-robin: uma tentativa por arquivo e
  retorno ao final da fila global.
- Cada PDF mantém estado independente das páginas concluídas, falhas, cooldowns
  e próxima tentativa; voltar para a fila não reinicia sua leitura.
- O progresso usa o total real de páginas de todos os PDFs do ZIP, evitando o
  status enganoso `0 de 1` enquanto os outros documentos ainda nem começaram.
- Regressão automatizada: primeiro PDF indisponível, segundo e terceiro avançam,
  e o primeiro retorna depois sem perda de resultado.
- Validação adicional executada com 15 PDFs reais/16 páginas do corpus: o primeiro
  arquivo falhou na chamada 1, voltou na chamada 16 e todos foram concluídos.

---

# 29. ATUALIZAÇÃO — CLASSIFICAÇÃO DE CÓDIGOS DE NFS-e

- O cadastro real contém 15 serviços; exemplo confirmado:
  `700186 = SERVICO DE CALIBRACAO`.
- A saída estruturada da leitura agora restringe `service_code` por enum aos
  códigos efetivamente carregados do Excel, além de vazio.
- Se uma NFS-e vier sem código válido, é executada uma segunda classificação
  textual focada apenas em prestador, descrição integral, evidência e catálogo.
- A segunda etapa faz correspondência semântica, não igualdade literal.
- Critérios importantes: calibração/aferição/metrologia → `700186`; hora-homem
  somente com cobrança explícita por horas/HH/mão de obra; manutenção/reparo sem
  horas explícitas → assistência técnica comum; transporte e destinação de
  resíduos permanecem separados; locações específicas prevalecem sobre a
  categoria genérica de equipamentos.
- O backend normaliza o código e só o aceita se ele existir no cadastro TOTVS.
- Se apenas a classificação secundária falhar temporariamente, a extração da
  página fica em cache; a fila repete somente a classificação, sem reler o PDF.
- Regressões adicionadas para o ciclo completo de calibração: classificação
  secundária, enum fechado, validação e preenchimento de código/descrição na revisão.
- Ensaio integrado adicional com 15 PDFs reais/16 documentos confirmou que a
  fila global continua funcionando quando todas as NFS-e precisam da segunda
  classificação e que `700186` chega preservado ao resultado de cada documento.

---

# 30. ATUALIZAÇÃO — CÓDIGO DO PRODUTO DO EMITENTE E EXPORTAÇÃO

- NF-e agora possui o campo `Código do Produto do Emitente`, separado do campo
  `Código no Banco de Dados`, que continua sendo a correlação com o TOTVS.
- A leitura estruturada solicita o código impresso na coluna de produto do
  DANFE e proíbe confundi-lo com NCM, CFOP, CST, quantidade ou número da nota.
- Códigos alfanuméricos e sua pontuação são preservados, como `ABC-001/XP`.
- O parser local/fallback também encaminha seu `codigo_fornecedor` para a mesma
  coluna da revisão.
- `Arquivo` e `Observações` continuam visíveis dentro do sistema, mas foram
  excluídos de todas as abas da exportação Excel.
- Validação automatizada: 35 testes do backend, build de produção do frontend e
  leitura local de dois DANFEs reais com códigos como `2026248` e `3656/1`.
