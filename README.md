# Mega App - Consulta de acesso

Aplicação web responsiva para consulta de acesso, vencimento e pagamento, com lembretes por notificação, preferências de aplicativo e métricas anônimas da comunidade.

## Recursos

- Consulta de login, senha, vencimento e link de pagamento.
- Lembretes configuráveis por notificação push.
- Preferências de aplicativo por tela.
- Contagem anônima de instalações e curtidas.
- Painel administrativo protegido por token.
- Interface PWA responsiva para desktop e celular.
- Clientes e revendas pesquisados no SQLite, com sincronizacao automatica da API The Best a cada 10 minutos.
- Lembretes, inscricoes push e contatos persistidos diretamente no SQLite.

## Executar localmente

1. Crie um ambiente virtual e instale as dependências:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copie `.env.example` para `.env` e preencha somente as configurações necessárias.

3. Inicie o servidor:

   ```powershell
   python -m uvicorn app:app --host 127.0.0.1 --port 8000
   ```

4. Acesse `http://127.0.0.1:8000`.

## Testes

```powershell
python -m unittest discover -s tests -v
```

## Sincronizar com Git e resetar a aplicacao

Quando houver alteracoes locais e voce quiser salvar no Git antes de atualizar a aplicacao com o que esta no remoto:

Comando rapido no Windows/PowerShell:

```powershell
.\atualizar.ps1
```

Comando rapido no Linux/VPS:

```bash
bash atualizar.sh
```

Linux/VPS:

```bash
bash scripts/git-sync-reset.sh "mensagem do commit"
```

Windows/PowerShell:

```powershell
.\scripts\git-sync-reset.ps1 "mensagem do commit"
```

O script faz commit das alteracoes locais, envia para `origin/main`, baixa as novidades e executa `git reset --hard origin/main`.

## Sincronizacao manual

O servico sincroniza automaticamente ao iniciar e a cada 10 minutos. Para executar uma carga manual:

```powershell
python scripts/sync_resellers.py
```

O servico de pagamento da porta 8080 continua sendo consultado somente para obter o link de pagamento,
pois esse campo nao existe na API The Best.

Dados locais, credenciais, bancos SQLite e arquivos de QA não são versionados.
