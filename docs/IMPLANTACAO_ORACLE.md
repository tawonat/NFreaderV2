# NFreader na Oracle Cloud Always Free

Este roteiro usa uma VM Ubuntu ARM, Docker Compose e Caddy. O domínio aponta para o frontend, que encaminha `/api` internamente ao backend. O banco de usuários fica em `data/usuarios.sqlite3` no disco da VM. As chaves e senhas não acompanham o ZIP.

## 1. Conta e máquina virtual

1. Cadastre-se em https://www.oracle.com/cloud/free/ . A Oracle pode pedir cartão para verificar a conta. Escolha com cuidado a região principal (home region), pois os recursos Always Free ficam nela. Se a forma A1 estiver sem capacidade, tente outro domínio de disponibilidade na mesma região ou novamente mais tarde.
2. No painel Oracle, use **Networking > Virtual Cloud Networks > Start VCN Wizard > Create VCN with Internet Connectivity**. Crie uma rede com subnet pública e internet gateway.
3. Vá a **Compute > Instances > Create instance**. Selecione **Ubuntu 24.04**, forma **VM.Standard.A1.Flex**, **2 OCPUs e 12 GB de RAM**, marcada **Always Free eligible**. Um boot volume de 50 GB cabe no limite gratuito, se o painel confirmar essa elegibilidade.
4. Escolha a VCN e sua subnet pública, atribua **Public IPv4 address**. Gere um par de chaves SSH e baixe a **private key** no computador. Nunca envie essa chave a outras pessoas.
5. Na security list ou no NSG da subnet, permita entrada TCP **22** somente do seu IP público (`x.x.x.x/32`), e TCP **80** e **443** de `0.0.0.0/0`. Não abra 3000 nem 8000. Confira também que há saída à internet, necessária para instalar dependências e chamar a API de leitura.
6. Anote o IP público da instância. O usuário SSH da imagem Ubuntu é `ubuntu`.

O limite Always Free da A1 pode mudar; confirme os indicadores de custo e elegibilidade no painel antes de criar a VM. A Oracle pode recuperar instâncias consideradas ociosas por um período prolongado. Mantenha backups do banco fora da VM.

## 2. Domínio gratuito

Crie um subdomínio em https://www.duckdns.org/ (por exemplo `nfreader-suaempresa.duckdns.org`) e aponte o registro para o **IP público da VM**. Pode usar um subdomínio da empresa se tiver acesso ao DNS; nesse caso crie um registro A para esse IP. Aguarde a resolução do domínio antes de iniciar o Caddy, que obterá o certificado HTTPS automaticamente. Se o IP da VM mudar, atualize o registro.

## 3. Instalar Docker na VM

No Windows PowerShell, conecte-se com a chave privada baixada:

```powershell
ssh -i "C:\caminho\sua-chave.key" ubuntu@SEU_IP_PUBLICO
```

Na VM, execute os comandos oficiais de instalação do Docker Engine para Ubuntu. São apt repository e pacotes da Docker; a documentação atualizada fica em https://docs.docker.com/engine/install/ubuntu/ . Para Ubuntu 24.04 ARM:

```bash
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo docker compose version
```

## 4. Enviar o projeto e configurar

Extraia o ZIP do NFreader no computador. Em **outro terminal PowerShell**, envie a pasta extraída (a pasta que contém `compose.yaml`, `back`, `front` e `Caddyfile`):

```powershell
scp -i "C:\caminho\sua-chave.key" -r "C:\caminho\NFreader" ubuntu@SEU_IP_PUBLICO:/home/ubuntu/nfreader
```

Confira que `/home/ubuntu/nfreader/compose.yaml` existe. De volta ao terminal SSH:

```bash
cd ~/nfreader
cp back/.env.example back/.env
nano back/.env
```

Troque `GEMINI_API_KEY=cole_sua_chave_aqui` pela chave real. O arquivo é privado. Configure o domínio na raiz:

```bash
printf 'NFREADER_DOMAIN=nfreader-suaempresa.duckdns.org\n' > .env
chmod 600 .env back/.env
mkdir -p data
sudo docker compose config >/dev/null
```

Substitua `nfreader-suaempresa.duckdns.org` pelo endereço criado. `back/.env.example` já define o modelo e as tentativas de leitura; ajuste-os somente se sua chave oferecer modelos diferentes. O ZIP não contém chave ou usuários.

## 5. Subir, criar usuário e acessar

```bash
cd ~/nfreader
sudo docker compose up -d --build
sudo docker compose ps
sudo docker compose exec backend python auth.py add
```

O `add` perguntará nome, senha e confirmação. Acesse `https://nfreader-suaempresa.duckdns.org` e entre com essa conta. O primeiro build pode demorar alguns minutos, principalmente na VM ARM. O HTTPS também pode levar um pouco após o DNS começar a resolver.

Se houver erro, veja:

```bash
sudo docker compose logs --tail=100 backend
sudo docker compose logs --tail=100 front
sudo docker compose logs --tail=100 caddy
```

Se o site não abrir, confira DNS, IP público, security list/NSG e portas 80/443. Se o frontend mostrar falha de proxy, confira o backend com `sudo docker compose ps` e seus logs. Os serviços iniciam automaticamente após reiniciar a VM, desde que o Docker também inicie.

## 6. Operação e backup

- O backend roda com **um worker**: análises e revisões em curso ficam na memória e se perdem se o processo/VM reiniciar. Finalize e exporte os documentos antes de atualizar ou reiniciar. A fila pode demorar com documentos grandes e a chave Gemini mantém seus limites próprios.
- Os usuários sobrevivem aos reinícios porque `./data` é persistente. Não compartilhe `back/.env`, `data/usuarios.sqlite3` ou a chave SSH. Faça backup externo periódico; o volume da VM não é um backup.
- Para criar/remover contas: `sudo docker compose exec backend python auth.py add` ou `sudo docker compose exec backend python auth.py remove NOME`.
- Para atualizar o código, envie a nova versão sem sobrescrever `.env`, `back/.env` e `data/`; depois execute `sudo docker compose up -d --build`. Faça isso quando não houver análise em andamento.

Para um backup consistente do SQLite, execute na VM:

```bash
sudo docker compose exec backend python -c 'import sqlite3; a=sqlite3.connect("/data/usuarios.sqlite3"); b=sqlite3.connect("/data/usuarios-backup.sqlite3"); a.backup(b); b.close(); a.close()'
sudo cp data/usuarios-backup.sqlite3 ~/usuarios-backup.sqlite3
sudo chown ubuntu:ubuntu ~/usuarios-backup.sqlite3
```

Baixe em um terminal PowerShell e guarde em local privado:

```powershell
scp -i "C:\caminho\sua-chave.key" ubuntu@SEU_IP_PUBLICO:/home/ubuntu/usuarios-backup.sqlite3 "C:\caminho\backups\usuarios-backup.sqlite3"
```
