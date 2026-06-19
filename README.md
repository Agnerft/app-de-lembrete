# Mega App - Consulta de acesso

Aplicação web responsiva para consulta de acesso, vencimento e pagamento, com lembretes por notificação, preferências de aplicativo e métricas anônimas da comunidade.

## Recursos

- Consulta de login, senha, vencimento e link de pagamento.
- Lembretes configuráveis por notificação push.
- Preferências de aplicativo por tela.
- Contagem anônima de instalações e curtidas.
- Painel administrativo protegido por token.
- Interface PWA responsiva para desktop e celular.

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

Dados locais, credenciais, bancos SQLite e arquivos de QA não são versionados.
