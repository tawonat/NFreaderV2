# NFreader V2

Ferramenta local do PCM para leitura, conferência e exportação de documentos.

As instruções de acesso estão em [ACESSO.md](ACESSO.md). O histórico das versões anteriores e os mapas de leitura descontinuados ficam em `historico/`. A pasta `frontend/` guarda instruções de desenvolvimento geradas pelo Next.js.

Para instalar em uma VM Oracle Cloud Always Free, siga [IMPLANTACAO_ORACLE.md](IMPLANTACAO_ORACLE.md).

## Motor de leitura Gemini

O modo normal usa a Gemini Developer API para interpretar visualmente NF-e,
NFS-e, CT-e, boletos e páginas de continuação. O frontend, a revisão, a
correlação com o cadastro TOTVS e a exportação para Excel permanecem iguais.

Antes do primeiro uso:

1. Crie uma chave da Gemini Developer API no Google AI Studio.
2. Execute `back\CONFIGURAR_GEMINI.cmd` e cole a chave.
3. Inicie ou reinicie o backend.

A chave fica somente no arquivo local `back\.env`, que não deve ser
compartilhado. Também é possível definir `GEMINI_API_KEY` diretamente nas
variáveis de ambiente. O modelo padrão é `gemini-3.5-flash-lite` e pode ser alterado
por `GEMINI_MODEL`. Em falhas transitórias, o backend alterna automaticamente
entre os modelos definidos em `GEMINI_FALLBACK_MODELS`. Eles são acessados pela
Interactions API.

Quando a chave estiver ausente, inválida, sem permissão ou a cota estiver
esgotada, o backend apresenta o motivo e não preenche campos com dados
possivelmente incorretos.

O modo `Deep Reading`, dedicado a orçamentos, preserva o motor local anterior.

## O que mudou na V2

### Processamento normal
- Leitura visual de NF-e e NFS-e pelo Gemini, com saída estruturada e evidências.
- A IA escolhe o código da NFS-e dentro do catálogo TOTVS; o backend valida o código antes de aceitá-lo.
- Notas da ANDAIMES ELIMAR recebem sempre o código `715237` (locação de andaimes).
- O campo de código usa uma lista fechada com os códigos existentes no cadastro.
  Quando a leitura principal não define um código válido, uma segunda classificação
  focada analisa somente a descrição do serviço e o catálogo antes da revisão.
- CT-e possui categoria própria, com código TOTVS fixo `780007`.
- Na NF-e, a quantidade de cada produto é mantida como texto exatamente com os separadores e zeros lidos na nota, inclusive na coluna do Excel.
- Boletos e documentos não identificados vão para `Outros`.
- Resultado provisório antes da geração do Excel.
- Tela de revisão com o documento PDF ao lado dos dados extraídos.
- Códigos e descrições do cadastro TOTVS aparecem para conferência.
- As principais correspondências encontradas ficam disponíveis para seleção.
- O Excel só é gerado depois da revisão.

### Deep Reading
Modo especial para orçamentos.

O processamento prioriza precisão em vez de velocidade, com resolução OCR adaptativa e múltiplas leituras internas. O resultado do orçamento contém somente:

- Nome no orçamento
- Código no banco de dados
- Descrição no orçamento
- Descrição no banco de dados

Quando a correspondência não alcança segurança suficiente, o sistema deixa o código pendente e apresenta alternativas para revisão.

## Estrutura

```text
NFreader V2/
├── back/
│   ├── main.py
│   ├── ocr_engine.py
│   ├── cadastro/
│   │   ├── produtos_totvs.xlsx
│   │   └── servicos_totvs.xlsx
│   └── requirements.txt
└── front/
    ├── package.json
    ├── package-lock.json
    └── src/
```

## Execução local no Windows

Antes de abrir o sistema, crie pelo menos um usuário no terminal, dentro de `back`:

```powershell
python auth.py add
```

A senha será solicitada no terminal. Veja [ACESSO.md](ACESSO.md) para administrar usuários e configurar a hospedagem.

### Backend

```powershell
cd "C:\caminho\NFreader V2\back"
python -m uvicorn main:app --reload
```

### Frontend

```powershell
cd "C:\caminho\NFreader V2\front"
& "C:\Users\otavio.seidinger\Desktop\NodeJS\npm.cmd" install
& "C:\Users\otavio.seidinger\Desktop\NodeJS\npm.cmd" run dev
```

O frontend usa `/api`, encaminhado pelo Next.js para `http://127.0.0.1:8000` por padrão.

## NFreader V2

A V2 adiciona revisão antes da exportação, confiança por campo, consulta manual aos cadastros TOTVS e Deep Reading dedicado a orçamentos. O Excel final possui as abas `Produtos (NF-e)`, `Serviços (NFS-e)`, `Transportes (CT-e)`, `Outros` e `Orçamentos`; a aba `Resumo` não é mais exportada. Percentuais, metadados de confiança, `Arquivo` e `Observações` permanecem apenas na revisão e não são exportados. Nas NF-e, `Código do Produto do Emitente` preserva o código impresso na nota em uma coluna separada de `Código no Banco de Dados` (TOTVS).

### Deep Reading

O modo Deep Reading prioriza precisão sobre velocidade. Para orçamentos, o sistema procura cada item/peça, correlaciona com o cadastro de produtos e extrai preço unitário, total da peça e total do pedido.


## PDFs com várias notas

Um único PDF pode conter muitas notas e documentos diferentes, inclusive documentos de várias páginas. No modo normal, o backend envia uma página por requisição ao Gemini e informa o contexto da página anterior. Em seguida, reúne novamente as páginas que a IA marcou como continuação do mesmo documento.

O backend valida a resposta, impede que uma página pertença a dois documentos e cria uma entrada de revisão para qualquer página que a IA não tenha atribuído. O PDF exibido na revisão continua contendo somente as páginas daquele documento.

Variáveis de operação:
- `GEMINI_API_KEY`: chave obrigatória no modo normal.
- `GEMINI_MODEL`: primeiro modelo utilizado (padrão `gemini-3.5-flash-lite`).
- `GEMINI_FALLBACK_MODELS`: modelos alternativos usados em 429/5xx ou timeout.
- `NFREADER_GEMINI_TIMEOUT`: limite em segundos por tentativa de página (padrão 90).
- `NFREADER_GEMINI_RETRIES`: tentativas em falhas transitórias 429/503 (padrão 4).
- `NFREADER_GEMINI_RETRY_DELAY`: espera inicial do retry em segundos (padrão 5).
- `NFREADER_GEMINI_PAGE_DELAY`: intervalo entre páginas para reduzir rajadas de requisições (padrão 8).
- `NFREADER_QUEUE_RETRY_DELAYS`: esperas sucessivas antes de recolocar uma página pendente em processamento (padrão `30,60,120,180`).
- `NFREADER_QUEUE_MAX_ATTEMPTS`: máximo de passagens por página; `0` mantém a fila tentando até concluir (padrão `0`).

Quando uma página encontra indisponibilidade transitória, ela vai para o fim da
fila e as páginas seguintes continuam. Resultados já concluídos permanecem em
memória e não são relidos. Ao final, as páginas são recolocadas na ordem original
antes do agrupamento de documentos e continuações.

Em arquivos ZIP, a fila também funciona entre PDFs. O processamento concede uma
tentativa a um arquivo e então passa ao próximo. Se uma página estiver aguardando,
o PDF preserva seu checkpoint e volta para o final da fila global, sem impedir a
leitura dos demais documentos do pacote.

## Compatibilidade de documentos

- A interpretação assume páginas retas, conforme o fluxo real de entrada.
- O Gemini recebe instruções explícitas para separar prestador de tomador, número da nota de RPS/série e valor final de bases e impostos.
- A resposta estruturada é validada antes de alimentar o frontend e a planilha.
- Classificações de NFS-e nunca aceitam códigos inventados: o valor precisa existir
  no cadastro carregado. Calibração/aferição, assistência técnica, hora-homem,
  transporte, resíduos e locações possuem critérios semânticos explícitos.
- A interface apresenta somente o andamento da leitura, sem expor o fornecedor ou a tecnologia usada pelo motor interno.
- DANFE, DACTE/CT-e e boletos possuem marcadores estruturais próprios para reduzir a classificação como `OUTRO`.
- Nomes de arquivos com caracteres Unicode são enviados com cabeçalho HTTP compatível, evitando erro ao abrir o PDF revisado.
