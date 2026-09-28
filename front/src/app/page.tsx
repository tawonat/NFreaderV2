"use client";

import { useEffect, useMemo, useState, type CSSProperties } from "react";

type Candidate = { codigo: string; descricao: string; score?: number };
type ConfidenceMap = Record<string, number>;
type Row = Record<string, any> & { id: string; document_id?: string; candidatos?: Candidate[]; confiancas?: ConfidenceMap };
type ApiResult = {
  session_id: string;
  modo: string;
  documentos: { id: string; arquivo: string; url: string }[];
  resumo: { nfe: number; nfse: number; cte: number; outros: number; orcamento: number };
  nfe_rows: Row[];
  nfse_rows: Row[];
  cte_rows: Row[];
  outros_rows: Row[];
  orcamento_rows: Row[];
};
type SearchResult = { codigo: string; descricao: string; centro_custos: string; ncm: string; unidade?: string; score?: number };

type Mode = "nfe" | "nfse" | "cte" | "orcamento";
const API = "/api";

const confidenceLabel = (value?: number) => {
  if (value === undefined || value === null) return "";
  if (value >= 81) return "Alta";
  if (value >= 70) return "Revisar";
  if (value >= 50) return "Baixa";
  return "Muito baixa";
};

function ConfidenceBadge({ value }: { value?: number }) {
  if (value === undefined || value === null) return null;
  return (
    <span className={`confidence-badge ${value >= 81 ? "high" : value >= 70 ? "medium" : value >= 50 ? "low" : "very-low"}`}>
      {Math.round(value)}% · {confidenceLabel(value)}
    </span>
  );
}

function RowEditor({
  row,
  mode,
  onChange,
  onSelectDocument,
  onOpenSearch,
  onCodeBlur,
}: {
  row: Row;
  mode: Mode;
  onChange: (next: Row) => void;
  onSelectDocument: (id: string) => void;
  onOpenSearch: (rowId: string) => void;
  onCodeBlur: (row: Row, mode: Mode) => void;
}) {
  const candidates = row.candidatos || [];
  const confidence = row.confiancas || {};
  const fields = mode === "orcamento"
    ? ["Nome no Orçamento", "Código no Banco de Dados", "Descrição no Orçamento", "Descrição no Banco de Dados", "Quantidade", "Preço Unitário", "Total da Peça", "Total do Pedido"]
    : mode === "nfe"
      ? ["Número da Nota", "Empresa", "Código do Produto do Emitente", "Descrição na Nota", "Quantidade", "Código no Banco de Dados", "Descrição no Banco de Dados", "Centro de Custos", "Observações"]
      : ["Número da Nota", "Empresa", "Descrição na Nota", "Quantidade", "Código no Banco de Dados", "Descrição no Banco de Dados", "Centro de Custos", "Valor Total", "Observações"];

  return (
    <div className={`review-card ${row._revisar ? "needs-review" : ""}`}>
      <div className="review-card-head">
        <div>
          <strong>{mode === "orcamento" ? row["Descrição no Orçamento"] || "Item do orçamento" : row["Descrição na Nota"] || "Item"}</strong>
          <span>{row.Arquivo || ""}</span>
        </div>
        <button className="small-ghost" onClick={() => row.document_id && onSelectDocument(row.document_id)}>Ver documento</button>
      </div>

      <div className="field-grid">
        {fields.map((field) => (
          <label key={field} className={`field ${["Descrição na Nota", "Descrição no Orçamento", "Descrição no Banco de Dados", "Observações"].includes(field) ? "wide" : ""}`}>
            <span className="field-label">
              <span>{field}</span>
              <ConfidenceBadge value={confidence[field]} />
            </span>
            <input
              value={row[field] ?? ""}
              onChange={(e) => onChange({ ...row, [field]: e.target.value })}
              onBlur={() => {
                if (field === "Código no Banco de Dados") onCodeBlur(row, mode);
              }}
            />
          </label>
        ))}
      </div>

      <div className="review-toolbar">
        <button className="secondary-btn" onClick={() => onOpenSearch(row.id)}>Pesquisar no banco TOTVS</button>
        <span className="review-status">Confira e corrija antes de exportar.</span>
      </div>

      {candidates.length > 0 && (
        <div className="candidates">
          <div className="candidate-title">Correspondências sugeridas</div>
          {candidates.map((c, index) => (
            <button
              key={`${c.codigo}-${index}`}
              className={`candidate ${index === 0 ? "candidate-best" : ""}`}
              onClick={() => onChange({
                ...row,
                "Código no Banco de Dados": c.codigo,
                "Descrição no Banco de Dados": c.descricao,
                confiancas: {
                  ...(row.confiancas || {}),
                  "Código no Banco de Dados": c.score ?? row.confiancas?.["Código no Banco de Dados"] ?? 0,
                  "Descrição no Banco de Dados": c.score ?? row.confiancas?.["Descrição no Banco de Dados"] ?? 0,
                },
              })}
            >
              <strong>{c.codigo}</strong>
              <span>{c.descricao}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Home() {
  const [usuario, setUsuario] = useState<string | null>(null);
  const [verificandoLogin, setVerificandoLogin] = useState(true);
  const [nomeLogin, setNomeLogin] = useState("");
  const [senhaLogin, setSenhaLogin] = useState("");
  const [erroLogin, setErroLogin] = useState("");
  const [enviandoLogin, setEnviandoLogin] = useState(false);
  useEffect(() => {
    fetch(`${API}/auth/me`, { cache: "no-store" })
      .then(async (r) => { if (r.ok) setUsuario((await r.json()).username); })
      .catch(() => setErroLogin("Não foi possível acessar o servidor."))
      .finally(() => setVerificandoLogin(false));
  }, []);

  const entrar = async (e: React.FormEvent) => {
    e.preventDefault(); setErroLogin(""); setEnviandoLogin(true);
    try {
      const r = await fetch(`${API}/auth/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: nomeLogin.trim(), password: senhaLogin }) });
      if (!r.ok) throw new Error((await r.json()).detail || "Não foi possível entrar.");
      setUsuario((await r.json()).username); setSenhaLogin("");
    } catch (err) { setErroLogin(err instanceof Error ? err.message : "Não foi possível entrar."); }
    finally { setEnviandoLogin(false); }
  };

  const sair = async () => {
    await fetch(`${API}/auth/logout`, { method: "POST" }).catch(() => {});
    setUsuario(null); setResultado(null); setArquivo(null); setErro("");
  };

  const apiFetch = async (url: string, options?: RequestInit) => {
    const response = await fetch(url, options);
    if (response.status === 401) {
      setUsuario(null); setResultado(null);
      throw new Error("Sessão expirada. Entre novamente.");
    }
    return response;
  };
  const [modo, setModo] = useState<"normal" | "deep">("normal");
  const [centroCusto, setCentroCusto] = useState("");
  const [arquivo, setArquivo] = useState<File | null>(null);
  const [resultado, setResultado] = useState<ApiResult | null>(null);
  const [aba, setAba] = useState<"nfe" | "nfse" | "cte" | "outros" | "orcamento" | "banco">("nfe");
  const [mostrarDocumento, setMostrarDocumento] = useState(true);
  const [loading, setLoading] = useState(false);
  const [exportando, setExportando] = useState(false);
  const [erro, setErro] = useState("");
  const [documentoSelecionado, setDocumentoSelecionado] = useState("");
  const [linhaPesquisaId, setLinhaPesquisaId] = useState("");
  const [modoOrigemPesquisa, setModoOrigemPesquisa] = useState<Mode | null>(null);
  const [mostrarBancoInicial, setMostrarBancoInicial] = useState(false);
  const [tipoPesquisa, setTipoPesquisa] = useState("produtos");
  const [termoPesquisa, setTermoPesquisa] = useState("");
  const [resultadosPesquisa, setResultadosPesquisa] = useState<SearchResult[]>([]);
  const [pesquisando, setPesquisando] = useState(false);
  const [progresso, setProgresso] = useState(0);
  const [progressoStatus, setProgressoStatus] = useState("Preparando documentos...");

  const linhasAtuais = useMemo(() => {
    if (!resultado) return [];
    if (aba === "nfe") return resultado.nfe_rows;
    if (aba === "nfse") return resultado.nfse_rows;
    if (aba === "cte") return resultado.cte_rows;
    if (aba === "outros") return resultado.outros_rows as Row[];
    return resultado.orcamento_rows;
  }, [resultado, aba]);

  const setRowsForMode = (mode: Mode, rows: Row[]) => {
    if (!resultado) return;
    const next = { ...resultado };
    if (mode === "nfe") next.nfe_rows = rows;
    else if (mode === "nfse") next.nfse_rows = rows;
    else if (mode === "cte") next.cte_rows = rows;
    else next.orcamento_rows = rows;
    setResultado(next);
  };

  const atualizarLinha = (next: Row, mode: Mode) => {
    if (!resultado) return;
    const rows = mode === "nfe" ? resultado.nfe_rows : mode === "nfse" ? resultado.nfse_rows : mode === "cte" ? resultado.cte_rows : resultado.orcamento_rows;
    setRowsForMode(mode, rows.map((r) => r.id === next.id ? next : r));
  };

  const analisar = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!arquivo) return setErro("Selecione um PDF ou ZIP para começar.");
    if (modo === "normal" && !centroCusto.trim()) return setErro("Informe o Centro de Custos.");
    setErro(""); setLoading(true); setResultado(null); setProgresso(0); setProgressoStatus(modo === "deep" ? "Preparando Deep Reading..." : "Preparando documentos...");
    const form = new FormData();
    form.append("centroCusto", centroCusto);
    form.append("modo", modo);
    form.append("notas", arquivo);
    try {
      const response = await apiFetch(`${API}/iniciar-analise`, { method: "POST", body: form });
      if (!response.ok) throw new Error(await response.text());
      const { job_id } = await response.json();
      while (true) {
        await new Promise((resolve) => setTimeout(resolve, 450));
        const statusResponse = await apiFetch(`${API}/progresso/${job_id}`);
        if (!statusResponse.ok) throw new Error(await statusResponse.text());
        const status = await statusResponse.json();
        setProgresso(Number(status.progress || 0));
        setProgressoStatus(status.current || "Processando documentos...");
        if (status.status === "done") {
          const data: ApiResult = status.resultado;
          setResultado(data);
          setDocumentoSelecionado(data.documentos[0]?.id || "");
          setLinhaPesquisaId("");
          setModoOrigemPesquisa(null);
          setMostrarBancoInicial(false);
          setMostrarDocumento(true);
          setAba(modo === "deep" ? "orcamento" : (data.nfe_rows.length ? "nfe" : data.nfse_rows.length ? "nfse" : data.cte_rows.length ? "cte" : "outros"));
          break;
        }
        if (status.status === "error") throw new Error(status.error || "Falha no processamento.");
      }
    } catch (err) {
      console.error(err);
      setErro("Não foi possível processar os arquivos. Verifique se o backend está rodando.");
    } finally { setLoading(false); }
  };

  const exportar = async () => {
    if (!resultado) return;
    setExportando(true); setErro("");
    try {
      const response = await apiFetch(`${API}/exportar`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: resultado.session_id,
          centro_custo: centroCusto,
          nfe_rows: resultado.nfe_rows,
          nfse_rows: resultado.nfse_rows,
          cte_rows: resultado.cte_rows,
          outros_rows: resultado.outros_rows,
          orcamento_rows: resultado.orcamento_rows,
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url; link.download = "Resultado_NFreader.xlsx";
      document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
    } catch (err) {
      console.error(err); setErro("Erro ao gerar o Excel final.");
    } finally { setExportando(false); }
  };

  const openDbSearch = (rowId: string) => {
    const origem: Mode = aba === "nfse" ? "nfse" : aba === "cte" ? "cte" : aba === "orcamento" ? "orcamento" : "nfe";
    const row = linhasAtuais.find((r) => r.id === rowId);
    setLinhaPesquisaId(rowId);
    setModoOrigemPesquisa(origem);
    setMostrarBancoInicial(false);
    setTipoPesquisa(origem === "nfse" || origem === "cte" ? "servicos" : "produtos");
    if (row) {
      const seed = row["Descrição no Banco de Dados"] || row["Descrição no Orçamento"] || row["Descrição na Nota"] || row["Nome no Orçamento"] || "";
      setTermoPesquisa(seed);
    }
    setAba("banco");
  };

  const openManualDbSearch = () => {
    setLinhaPesquisaId("");
    setModoOrigemPesquisa(null);
    setMostrarBancoInicial(true);
    setResultadosPesquisa([]);
    setTermoPesquisa("");
    setAba("banco");
  };

  const pesquisarBanco = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (termoPesquisa.trim().length < 2) return setErro("Digite pelo menos 2 caracteres para pesquisar no banco.");
    setErro(""); setPesquisando(true);
    try {
      const response = await apiFetch(`${API}/cadastro/pesquisar?tipo=${encodeURIComponent(tipoPesquisa)}&q=${encodeURIComponent(termoPesquisa)}&limite=60`);
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      setResultadosPesquisa(data.resultados || []);
    } catch (err) {
      console.error(err); setErro("Não foi possível pesquisar o cadastro TOTVS.");
    } finally { setPesquisando(false); }
  };

  const aplicarResultadoBanco = (result: SearchResult) => {
    if (!resultado || !linhaPesquisaId) return;
    const sourceMode: Mode = modoOrigemPesquisa || "nfe";
    const rows = sourceMode === "nfe" ? resultado.nfe_rows : sourceMode === "nfse" ? resultado.nfse_rows : sourceMode === "cte" ? resultado.cte_rows : resultado.orcamento_rows;
    const nextRows = rows.map((row) => {
      if (row.id !== linhaPesquisaId) return row;
      return {
        ...row,
        "Código no Banco de Dados": result.codigo,
        "Descrição no Banco de Dados": result.descricao,
        ...(sourceMode !== "orcamento" ? { "Centro de Custos": row["Centro de Custos"] || result.centro_custos } : {}),
        confiancas: {
          ...(row.confiancas || {}),
          "Código no Banco de Dados": Math.max(90, row.confiancas?.["Código no Banco de Dados"] || 0),
          "Descrição no Banco de Dados": Math.max(90, row.confiancas?.["Descrição no Banco de Dados"] || 0),
        },
      };
    });
    setRowsForMode(sourceMode, nextRows);
    setModoOrigemPesquisa(null);
    setLinhaPesquisaId("");
    setMostrarBancoInicial(false);
    setAba(sourceMode);
  };

  const preencherDescricaoPorCodigo = async (row: Row, mode: Mode) => {
    const codigo = String(row["Código no Banco de Dados"] ?? "").trim();
    if (!codigo) return;
    try {
      const tipo = mode === "nfse" || mode === "cte" ? "servicos" : "produtos";
      const response = await apiFetch(`${API}/cadastro/codigo?tipo=${encodeURIComponent(tipo)}&codigo=${encodeURIComponent(codigo)}`);
      if (!response.ok) return;
      const data = await response.json();
      if (!data.encontrado || !data.resultado) return;
      const r = data.resultado as SearchResult;
      const next: Row = {
        ...row,
        "Descrição no Banco de Dados": r.descricao,
        ...(mode !== "orcamento" && !row["Centro de Custos"] && r.centro_custos ? { "Centro de Custos": r.centro_custos } : {}),
        confiancas: {
          ...(row.confiancas || {}),
          "Código no Banco de Dados": Math.max(95, row.confiancas?.["Código no Banco de Dados"] || 0),
          "Descrição no Banco de Dados": Math.max(95, row.confiancas?.["Descrição no Banco de Dados"] || 0),
        },
      };
      atualizarLinha(next, mode);
    } catch (err) {
      console.debug("Não foi possível consultar o código no cadastro TOTVS", err);
    }
  };

  if (verificandoLogin) return <main className="login-shell"><div className="login-card">Verificando acesso...</div></main>;
  if (!usuario) return (
    <main className="login-shell">
      <section className="login-card">
        <img className="hercules-logo" src="/hercules-logo.png" alt="Hércules" />
        <div className="eyebrow">NFreader · PCM</div>
        <h1>Acesso ao sistema</h1>
        <p>Entre com o usuário e a senha fornecidos pelo administrador.</p>
        <form onSubmit={entrar} className="login-form">
          <label className="field"><span>Usuário</span><input autoComplete="username" required value={nomeLogin} onChange={(e) => setNomeLogin(e.target.value)} /></label>
          <label className="field"><span>Senha</span><input type="password" autoComplete="current-password" required value={senhaLogin} onChange={(e) => setSenhaLogin(e.target.value)} /></label>
          <button type="submit" className="primary-btn" disabled={enviandoLogin}>{enviandoLogin ? "Entrando..." : "Entrar"}</button>
          {erroLogin && <div className="error-box" role="alert">{erroLogin}</div>}
        </form>
      </section>
    </main>
  );

  return (
    <main className="app-shell">
      <section className="app-card">
        <header className="app-header">
          <div className="header-brand-area">
            <img className="hercules-logo" src="/hercules-logo.png" alt="Hércules" />
            <div>
              <div className="eyebrow">NFreader · PCM</div>
              <h1>Leitura e conferência documental</h1>
              <p>Processamento local com revisão antes da exportação.</p>
            </div>
          </div>
          <div className="header-actions">
            <span>{usuario}</span>
            {resultado && <button className="ghost-btn" onClick={() => { setResultado(null); setAba("nfe"); setProgresso(0); setProgressoStatus("Preparando documentos..."); }}>Novo processamento</button>}
            <button className="ghost-btn" onClick={sair}>Sair</button>
          </div>
        </header>

        {!resultado && (
          <>
          <form onSubmit={analisar} className="setup-form">
            <div className="mode-grid">
              <button type="button" className={`mode-card ${modo === "normal" ? "selected" : ""}`} onClick={() => setModo("normal")}>
                <span className="mode-kicker">Modo normal</span>
                <strong>NF-e e NFS-e</strong>
                <small>Leitura, correlação e revisão com o fluxo padrão.</small>
              </button>
              <button type="button" className={`mode-card deep ${modo === "deep" ? "selected" : ""}`} onClick={() => setModo("deep")}>
                <span className="mode-kicker">Modo especial</span>
                <strong>Deep Reading</strong>
                <small>Orçamentos · máxima precisão, sem prioridade de velocidade.</small>
              </button>
            </div>

            {modo === "normal" && (
              <label className="field large">
                <span>Centro de Custos Total</span>
                <input value={centroCusto} onChange={(e) => setCentroCusto(e.target.value)} placeholder="Ex.: 102030 - Manutenção" />
              </label>
            )}

            <label className="upload-box">
              <span>{modo === "deep" ? "Selecione o orçamento" : "Selecione as notas"}</span>
              <strong>{arquivo ? arquivo.name : "PDF ou ZIP"}</strong>
              <small>O cadastro TOTVS fixo já está incorporado ao sistema.</small>
              <input type="file" accept=".pdf,.zip" onChange={(e) => setArquivo(e.target.files?.[0] || null)} />
            </label>

            <button className={`primary-btn ${modo === "deep" ? "deep-btn" : ""}`} disabled={loading} type="submit">
              {loading ? (modo === "deep" ? "Executando Deep Reading..." : "Analisando documentos...") : (modo === "deep" ? "Iniciar Deep Reading" : "Iniciar processamento")}
            </button>
            {loading && (
              <div className="progress-box" aria-live="polite">
                <div className="progress-ring-wrap">
                  <div className="progress-ring" style={{ background: `conic-gradient(var(--hercules-blue) ${progresso * 3.6}deg, #e6ebf1 0deg)` } as CSSProperties}>
                    <div className="progress-ring-inner">
                      <strong>{progresso}%</strong>
                      <span>concluído</span>
                    </div>
                  </div>
                </div>
                <div className="progress-copy">
                  <strong>{modo === "deep" ? "Deep Reading em andamento" : "Leitura em andamento"}</strong>
                  <p>{progressoStatus}</p>
                  <small>O processamento continua mesmo quando uma etapa exige mais tempo de análise.</small>
                </div>
              </div>
            )}
            <button type="button" className="db-entry-card" onClick={openManualDbSearch}>
              <span className="mode-kicker">Consulta manual</span>
              <strong>Pesquisar no banco TOTVS</strong>
              <small>Consulte diretamente os cadastros de produtos e serviços sem precisar processar uma nota.</small>
            </button>
            {erro && <div className="error-box">{erro}</div>}
          </form>

          {mostrarBancoInicial && (
            <section className="db-screen initial-db-screen">
              <div className="review-intro">
                <div>
                  <strong>Pesquisar no banco TOTVS</strong>
                  <p>Consulta manual do cadastro completo incorporado ao NFreader.</p>
                </div>
                <button className="ghost-btn" onClick={() => setMostrarBancoInicial(false)}>Fechar consulta</button>
              </div>

              <form className="db-search-form" onSubmit={pesquisarBanco}>
                <label className="field">
                  <span>Cadastro</span>
                  <select value={tipoPesquisa} onChange={(e) => setTipoPesquisa(e.target.value)}>
                    <option value="produtos">Produtos / NF-e</option>
                    <option value="servicos">Serviços / NFS-e</option>
                  </select>
                </label>
                <label className="field search-field">
                  <span>Palavras-chave</span>
                  <input value={termoPesquisa} onChange={(e) => setTermoPesquisa(e.target.value)} placeholder="Ex.: tubo aço inox 304" />
                </label>
                <button className="primary-btn search-button" disabled={pesquisando} type="submit">{pesquisando ? "Pesquisando..." : "Pesquisar"}</button>
              </form>

              <div className="db-results">
                {resultadosPesquisa.length === 0 ? (
                  <div className="empty-state">Pesquise por código, palavra ou trecho da descrição.</div>
                ) : resultadosPesquisa.map((r, i) => (
                  <div className="db-result" key={`${r.codigo}-${i}`}>
                    <div className="db-result-main">
                      <strong>{r.codigo}</strong>
                      <span>{r.descricao}</span>
                    </div>
                    <div className="db-result-meta">
                      {r.unidade && <span>Unid.: {r.unidade}</span>}
                      {r.centro_custos && <span>Centro: {r.centro_custos}</span>}
                      {r.ncm && <span>NCM: {r.ncm}</span>}
                    </div>
                  </div>
                ))}
              </div>
              {erro && <div className="error-box">{erro}</div>}
            </section>
          )}
          </>
        )}

        {resultado && (
          <div className="review-screen">
            <div className="summary-grid">
              <div><span>NF-e</span><strong>{resultado.resumo.nfe}</strong></div>
              <div><span>NFS-e</span><strong>{resultado.resumo.nfse}</strong></div>
              <div><span>CT-e</span><strong>{resultado.resumo.cte}</strong></div>
              <div><span>Outros</span><strong>{resultado.resumo.outros}</strong></div>
              <div><span>Orçamentos</span><strong>{resultado.resumo.orcamento}</strong></div>
            </div>

            <div className="tabs">
              <button className={aba === "nfe" ? "active" : ""} onClick={() => setAba("nfe")}>NF-e ({resultado.nfe_rows.length})</button>
              <button className={aba === "nfse" ? "active" : ""} onClick={() => setAba("nfse")}>NFS-e ({resultado.nfse_rows.length})</button>
              <button className={aba === "cte" ? "active" : ""} onClick={() => setAba("cte")}>CT-e ({resultado.cte_rows.length})</button>
              <button className={aba === "orcamento" ? "active" : ""} onClick={() => setAba("orcamento")}>Orçamentos ({resultado.orcamento_rows.length})</button>
              <button className={aba === "outros" ? "active" : ""} onClick={() => setAba("outros")}>Outros ({resultado.outros_rows.length})</button>
              <button className={aba === "banco" ? "active" : ""} onClick={() => { setAba("banco"); setLinhaPesquisaId(""); setModoOrigemPesquisa(null); setMostrarBancoInicial(false); }}>Banco TOTVS</button>
            </div>

            {aba === "banco" ? (
              <section className="db-screen">
                <div className="review-intro">
                  <div>
                    <strong>Pesquisar no banco TOTVS</strong>
                    <p>Use palavras-chave para localizar manualmente produtos ou serviços. O cadastro completo permanece dentro do sistema.</p>
                  </div>
                  <button className="export-btn" onClick={exportar} disabled={exportando}>{exportando ? "Gerando..." : "Exportar Excel"}</button>
                </div>

                <form className="db-search-form" onSubmit={pesquisarBanco}>
                  <label className="field">
                    <span>Cadastro</span>
                    <select value={tipoPesquisa} onChange={(e) => setTipoPesquisa(e.target.value)}>
                      <option value="produtos">Produtos / NF-e</option>
                      <option value="servicos">Serviços / NFS-e</option>
                    </select>
                  </label>
                  <label className="field search-field">
                    <span>Palavras-chave</span>
                    <input value={termoPesquisa} onChange={(e) => setTermoPesquisa(e.target.value)} placeholder="Ex.: tubo aço inox 304" />
                  </label>
                  <button className="primary-btn search-button" disabled={pesquisando} type="submit">{pesquisando ? "Pesquisando..." : "Pesquisar"}</button>
                </form>

                {linhaPesquisaId && <div className="target-notice">Item em revisão selecionado. Ao clicar em <strong>Usar neste item</strong>, o código e a descrição do TOTVS serão aplicados diretamente a ele.</div>}

                <div className="db-results">
                  {resultadosPesquisa.length === 0 ? (
                    <div className="empty-state">Pesquise por código, palavra ou trecho da descrição.</div>
                  ) : resultadosPesquisa.map((r, i) => (
                    <div className="db-result" key={`${r.codigo}-${i}`}>
                      <div className="db-result-main">
                        <strong>{r.codigo}</strong>
                        <span>{r.descricao}</span>
                      </div>
                      <div className="db-result-meta">
                        {r.unidade && <span>Unid.: {r.unidade}</span>}
                        {r.centro_custos && <span>Centro: {r.centro_custos}</span>}
                        {r.ncm && <span>NCM: {r.ncm}</span>}
                      </div>
                      {linhaPesquisaId && <button className="secondary-btn" onClick={() => aplicarResultadoBanco(r)}>Usar neste item</button>}
                    </div>
                  ))}
                </div>
                {erro && <div className="error-box">{erro}</div>}
              </section>
            ) : (
              <>
                <div className="review-intro">
                  <div>
                    <strong>{modo === "deep" ? "Revisão do orçamento" : "Revisão dos resultados"}</strong>
                    <p>Confira os dados ao lado da fonte antes de gerar o Excel definitivo.</p>
                  </div>
                  <button className="export-btn" onClick={exportar} disabled={exportando}>{exportando ? "Gerando..." : "Exportar Excel"}</button>
                </div>

                <div className={`review-layout ${mostrarDocumento ? "with-document" : "full-width"}`}>
                  {mostrarDocumento && (
                    <aside className="document-panel">
                      <div className="panel-head">
                        <div>
                          <span className="panel-kicker">Fonte</span>
                          <strong>Documento</strong>
                        </div>
                        <button type="button" className="icon-btn" title="Fechar documento" onClick={() => setMostrarDocumento(false)}>×</button>
                      </div>
                      <select className="doc-select" value={documentoSelecionado} onChange={(e) => setDocumentoSelecionado(e.target.value)}>
                        {resultado.documentos.map((doc) => <option key={doc.id} value={doc.id}>{doc.arquivo}</option>)}
                      </select>
                      {resultado.documentos.filter((doc) => doc.id === documentoSelecionado).map((doc) => (
                        <div key={doc.id} className="doc-entry">
                          <div className="doc-name">{doc.arquivo}</div>
                          <iframe title={doc.arquivo} src={`${API}${doc.url}`} />
                        </div>
                      ))}
                    </aside>
                  )}
                  <div className="results-panel">
                    <div className="results-toolbar">
                      {!mostrarDocumento && <button type="button" className="secondary-btn" onClick={() => setMostrarDocumento(true)}>Abrir documento</button>}
                      <span>{linhasAtuais.length} registro(s)</span>
                    </div>
                    {linhasAtuais.length === 0 ? (
                      <div className="empty-state">Nenhum resultado nesta aba.</div>
                    ) : aba === "outros" ? (
                      (linhasAtuais as Row[]).map((row, i) => <div className="simple-row" key={row.id || i}><strong>{row.Arquivo}</strong><span>{row["Tipo Detectado"]}</span><p>{row.Motivo}</p></div>)
                    ) : (
                      linhasAtuais.map((row) => (
                        <RowEditor key={row.id} row={row} mode={aba as Mode} onSelectDocument={setDocumentoSelecionado} onOpenSearch={openDbSearch} onCodeBlur={preencherDescricaoPorCodigo} onChange={(next) => atualizarLinha(next, aba as Mode)} />
                      ))
                    )}
                  </div>
                </div>
                {erro && <div className="error-box">{erro}</div>}
              </>
            )}
          </div>
        )}
      </section>
    </main>
  );
}
