# Acesso ao NFreader

O sistema inicia sem usuários. As contas ficam em `back/data/usuarios.sqlite3` e as senhas ficam protegidas por hash; não inclua esse arquivo no Git ou no ZIP distribuído.

No terminal, dentro de `back` e com o ambiente Python do projeto ativado:

```bash
python auth.py add
python auth.py list
python auth.py remove seu_usuario
```

O comando `add` solicita nome e senha no terminal (mínimo de 6 caracteres). Também é possível informar o nome diretamente: `python auth.py add seu_usuario`. A senha não aparece enquanto você digita. Adicionar um usuário existente altera a senha e encerra suas sessões. Crie os usuários diretamente no servidor em que o aplicativo vai rodar. Faça backup privado do arquivo SQLite se precisar conservar as contas.

Inicie o backend com `python -m uvicorn main:app --host 127.0.0.1 --port 8000` e o front com `npm run dev`. O front encaminha as chamadas `/api` ao backend. Para outro endereço, defina `NFREADER_BACKEND_URL` no ambiente do front antes do build/início. Em hospedagem HTTPS, defina `NFREADER_SECURE_COOKIE=1` no backend, publique o site por HTTPS e mantenha o SQLite em um volume persistente. Configure o serviço do backend para aceitar tráfego apenas do proxy/front quando possível.

Se o terminal do front mostrar `ECONNREFUSED 127.0.0.1:8000`, o backend não está atendendo nessa porta. Inicie o backend em outro terminal e confira seu endereço com `curl.exe -i http://127.0.0.1:8000/auth/me` no Windows: `401 Faça login` significa que ele está acessível. Se usar outra porta, defina `NFREADER_BACKEND_URL` no terminal do front e reinicie o Next.js.

Cada sessão expira em 12 horas. A API exige sessão também para processamento, progresso, PDF, consulta ao cadastro e exportação. A aplicação ainda guarda análises e documentos em memória, então reiniciar o backend descarta revisões em andamento.
