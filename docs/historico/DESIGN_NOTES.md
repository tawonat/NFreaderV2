# NFreader V2 — identidade e UX

- Identidade visual centrada na Leite Hércules, usando a logo fornecida pela empresa.
- Estilo corporativo + moderno, enxuto e sem excesso de cards.
- Revisão com painel PDF recolhível e reaberto por ação explícita.
- Confiança percentual por campo aparece somente durante a revisão, para NF-e, NFS-e e Orçamentos.
- Consulta manual ao banco TOTVS é uma tela própria e só é aberta quando o usuário aciona o botão correspondente.
- Campos da revisão permanecem editáveis manualmente.

# Evolução documental

- Um PDF deixou de ser tratado como necessariamente um único documento.
- A etapa de segmentação analisa página por página, identifica o tipo provável e agrupa páginas de continuidade antes do processamento fiscal.
- PDFs mistos podem conter NF-e, NFS-e e outros documentos no mesmo arquivo.
- Páginas de continuação podem ser processadas juntas com a nota de origem.
- Documentos fora do escopo continuam em `Outros` em vez de serem forçados para NF-e/NFS-e.
