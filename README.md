# Zabbix Report Automation

Automação em Python que captura dashboards e gráficos do **Zabbix**, gera um **relatório em PDF por cliente** e o envia por **e-mail** para cada empresa parceira, todo domingo às 23:50.

Não exige plugins nem alterações no servidor Zabbix: um navegador headless (Playwright/Chromium) acessa a interface web, o PDF é montado com `fpdf2` e o envio usa `smtplib`.

## Como funciona

1. Faz login no Zabbix (uma vez por execução).
2. Para cada cliente de `clients.json`: abre o dashboard e/ou os gráficos do host com o período de 7 dias na URL (`from=now-7d&to=now`), espera a renderização, captura as imagens e gera `output/AAAA-MM-DD/relatorio_<cliente>.pdf` (capa, dashboards e gráficos individuais, 3 por página).
3. Envia o PDF **somente** para os e-mails desse cliente, com assunto e corpo personalizados com o nome da empresa.
4. Se um cliente falhar (captura ou envio), registra o erro no log e **continua com os próximos**. No fim grava um resumo e termina com código 1 se houve falha.

## Estrutura

```
zabbix_report_agent.py   # script principal
install.sh               # instalador + agendamento no cron
requirements.txt         # dependências Python (versões mínimas)
requirements-lock.txt    # versões exatas testadas (opcional)
.env.example             # modelo das credenciais/parâmetros globais
clients.example.json     # modelo da lista de clientes
.env                     # credenciais reais      (NÃO versionar)
clients.json             # clientes reais         (NÃO versionar)
output/AAAA-MM-DD/       # PDFs gerados
logs/                    # zabbix_report.log e cron.log
```

## Instalação

Requisitos: Ubuntu/Debian com acesso à internet, Python 3.10+, acesso HTTPS ao Zabbix e a um servidor SMTP.

```bash
unzip zabbix_report_automation.zip      # ou: git clone <repo>
cd zabbix_report_automation
./install.sh
```

O `install.sh` (pode ser executado várias vezes):

1. atualiza os pacotes do sistema (`apt update && upgrade`);
2. instala `python3`, `python3-venv`, `pip`, `cron` e as bibliotecas do Chromium (`libnss3`, `libgbm1`, etc.);
3. cria o `venv` e instala o `requirements.txt`;
4. executa `playwright install chromium --with-deps` (se rodar sem root, instala as libs via `sudo` e o navegador para o seu usuário);
5. cria `.env` e `clients.json` a partir dos exemplos (com `chmod 600`), além de `logs/` e `output/`;
6. cria no crontab **todo domingo às 23:50**, gravando a saída em `logs/cron.log`.

Opções: `--no-cron` (não agenda) e `--no-deps` (não usa apt/sudo).

> Execute o `install.sh` com o **usuário que vai rodar o relatório**: o crontab e o Chromium ficam associados a ele. Evite alternar entre root e usuário comum no mesmo diretório (gera erros de permissão em `output/` e `.env`).

Para recriar apenas o venv com as versões exatas testadas: `pip install -r requirements-lock.txt`.

## Configuração

### 1. Credenciais e parâmetros globais — `.env`

```bash
nano .env      # modelo: .env.example
```

| Variável | Descrição |
|---|---|
| `ZABBIX_BASE_URL` | URL base, ex.: `https://zabbix.exemplo.com.br/zabbix` |
| `ZABBIX_USER` / `ZABBIX_PASSWORD` | Usuário e senha do Zabbix (use um usuário **somente leitura**) |
| `ZABBIX_VERIFY_TLS` | Valida o certificado do Zabbix (`true`/`false`) |
| `CLIENTS_FILE` | Arquivo de clientes (padrão `./clients.json`) |
| `ZABBIX_TIME_FROM` / `ZABBIX_TIME_TO` | Período (padrão `now-7d` / `now`) |
| `ZABBIX_EXTRA_WAIT_SECONDS` | Pausa extra após o `networkidle` para os gráficos renderizarem |
| `ZABBIX_CHARTS_PER_PAGE` | Gráficos por página do PDF (padrão `3`) |
| `ZABBIX_CHARTS_MAX` | Máximo de gráficos por host (`0` = todos) |
| `ZABBIX_CHARTS_NAME_FILTER` | Filtra os gráficos pelo nome (ex.: `CPU`) |
| `ZABBIX_VIEWPORT_WIDTH` / `_HEIGHT` / `ZABBIX_KIOSK` | Tamanho do navegador e modo kiosk |
| `REPORT_TITLE` | Título da capa. `{cliente}` vira o nome da empresa |
| `REPORT_OUTPUT_DIR` / `REPORT_RETENTION_DAYS` | Pasta dos PDFs e dias de retenção (`0` = nunca apagar) |
| `LOG_FILE` | Arquivo de log (padrão `./logs/zabbix_report.log`) |
| `SMTP_HOST` / `SMTP_PORT` | Servidor SMTP (use o hostname que consta no certificado) |
| `SMTP_SECURITY` | `starttls`, `ssl` ou `none` |
| `SMTP_VERIFY_TLS` | Valida o certificado do SMTP |
| `SMTP_USER` / `SMTP_PASSWORD` / `MAIL_FROM` | Credenciais e remetente |
| `MAIL_SUBJECT` | Assunto padrão. `{cliente}` vira o nome da empresa |
| `MAIL_TO_OVERRIDE` | **Só para testes**: manda todos os e-mails para este endereço |

### 2. Lista de clientes — `clients.json`

```bash
nano clients.json      # modelo: clients.example.json
```

```json
{
  "clients": [
    {
      "name": "Client",
      "slug": "client",
      "enabled": true,
      "dashboards": [
        {"title": "Relatório Client",
         "url": "https://zabbix.exemplo.com.br/zabbix/zabbix.php?action=dashboard.view&dashboardid=001"}
      ],
      "chart_hosts": [
        {"title": "Servidor Client", "id": 123456}
      ],
      "mail_to": ["client@example.com.br"],
      "mail_cc": []
    }
  ]
}
```

| Campo | Descrição |
|---|---|
| `name` | Nome da empresa (assunto, corpo do e-mail e capa do PDF) |
| `slug` | Opcional. Nome do arquivo: `relatorio_<slug>.pdf` |
| `enabled` | `false` ignora o cliente sem apagá-lo |
| `dashboards` | Dashboards do cliente: `{"url": ...}` ou `{"id": ...}`, com `title` opcional |
| `chart_hosts` | Hosts cujos gráficos individuais entram no PDF: `url` da página de gráficos ou `id` do host |
| `mail_to` / `mail_cc` | Destinatários **desse cliente** |
| `subject` | Opcional. Sobrescreve o `MAIL_SUBJECT` para esse cliente |

O cliente precisa ter ao menos um item em `dashboards` ou `chart_hosts`. O período vem sempre do `.env`.

## Como adicionar um novo cliente

1. No Zabbix, abra o dashboard do cliente e copie o número após `dashboardid=` na URL. Para os gráficos do agent, abra *Monitoramento → Hosts → Gráficos* e copie o `filter_hostids[0]` (ou cole a URL inteira).
2. Confirme que o usuário do `.env` tem permissão de leitura nesses dashboards e hosts.
3. Acrescente um bloco em `clients.json` (copie um existente):
   ```json
   {
     "name": "Clinet2",
     "dashboards": [{"title": "Relatório Client2", "id": 123}],
     "chart_hosts": [{"title": "Servidor Client2", "id": 456}],
     "mail_to": ["client2@example.com.br"]
   }
   ```
4. Teste só esse cliente, enviando para você (veja abaixo).
5. Pronto: o cron já processa todos os clientes habilitados, sem alterar mais nada.

## Execução e testes

```bash
venv/bin/python zabbix_report_agent.py              # todos os clientes
venv/bin/python zabbix_report_agent.py atlasnet     # só um cliente (slug)

# teste sem enviar ao cliente: todos os e-mails vão para você
MAIL_TO_OVERRIDE=seu.email@exemplo.com venv/bin/python zabbix_report_agent.py
```

Confira o PDF em `output/AAAA-MM-DD/`, o e-mail recebido e o código de saída (`echo $?`: 0 = tudo OK, 1 = algum cliente falhou).

## Agendamento (crontab)

O `install.sh` cria a entrada abaixo. Para criar manualmente, `crontab -e` (ajuste o caminho):

```cron
50 23 * * 0 cd /opt/zabbix_report_automation && /opt/zabbix_report_automation/venv/bin/python /opt/zabbix_report_automation/zabbix_report_agent.py >> /opt/zabbix_report_automation/logs/cron.log 2>&1
```

Campos: minuto `50`, hora `23`, qualquer dia do mês, qualquer mês, dia da semana `0` (domingo). Confira com `crontab -l`. A máquina precisa estar ligada, com o serviço `cron` ativo, e no fuso correto (`timedatectl set-timezone America/Sao_Paulo`).

## Logs

| Arquivo | Conteúdo |
|---|---|
| `logs/zabbix_report.log` | Início/fim de cada execução, capturas, **cada envio** (`ENVIO OK` com cliente, assunto, anexo e destinatários), **erros com traceback** e o resumo final. Rotativo (2 MB × 10) |
| `logs/cron.log` | Saída bruta do cron (pega falhas anteriores ao log, como erro de Python/venv) |

```bash
tail -f logs/zabbix_report.log
grep -E "ENVIO OK|ERROR|FALHA|Resumo" logs/zabbix_report.log
```

## Solução de problemas

| Sintoma | Causa provável / solução |
|---|---|
| `Falha no login do Zabbix` | Usuário/senha incorretos no `.env` |
| `retornou erro do Zabbix: No permissions to referred object...` | ID inexistente ou usuário sem permissão |
| Gráficos em branco no PDF | Aumente `ZABBIX_EXTRA_WAIT_SECONDS` |
| `Executable doesn't exist` (Playwright) | Rode `playwright install chromium` com o **mesmo usuário** que executa o script |
| Chromium não abre / libs ausentes | `sudo venv/bin/playwright install-deps chromium` |
| `CERTIFICATE_VERIFY_FAILED ... Hostname mismatch` (SMTP) | Use em `SMTP_HOST` o nome do certificado do servidor |
| `535 Incorrect authentication data` | `SMTP_USER`/`SMTP_PASSWORD` errados (ou é preciso senha de aplicativo) |
| Cron não executa | `systemctl status cron`, `crontab -l`, veja `logs/cron.log` |
| `PermissionError` | Execuções por usuários diferentes. Ajuste com `chown -R usuario: .` |

Os seletores de login (`#name`, `#password`, `#enter`) são os do Zabbix 5.x–7.x; em outras versões ajuste `_login` em `zabbix_report_agent.py`.

## Segurança

- `.env` e `clients.json` ficam fora do Git (`.gitignore`) e com permissão `600`.
- **Nunca coloque credenciais reais no `.env.example`.** Ele é o único arquivo de configuração versionado.
- Use um usuário Zabbix de leitura exclusivo e uma conta SMTP dedicada.
- Não desative `ZABBIX_VERIFY_TLS`/`SMTP_VERIFY_TLS` em produção.

## Publicando no GitHub

```bash
git init && git add .
git status          # confirme que .env, clients.json, venv/, logs/ e output/ NÃO aparecem
git commit -m "Zabbix Report Automation"
git branch -M main
git remote add origin git@github.com:SEU_USUARIO/zabbix_report_automation.git
git push -u origin main
```
