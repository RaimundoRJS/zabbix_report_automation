#!/usr/bin/env bash
# Instalador do Zabbix Report Automation (Ubuntu/Debian).
# Uso: ./install.sh [--no-cron] [--no-deps]
#   --no-cron  não cria o agendamento no crontab
#   --no-deps  não usa apt/sudo (pula update, pacotes do sistema e libs do Chromium)
# Execute com o usuário que vai rodar o relatório (o crontab e o Chromium
# ficam associados a ele). Pode ser executado várias vezes (idempotente).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRON_SCHEDULE="50 23 * * 0"   # todo domingo às 23:50
INSTALL_CRON=1
INSTALL_DEPS=1
for arg in "$@"; do
  case "$arg" in
    --no-cron) INSTALL_CRON=0 ;;
    --no-deps) INSTALL_DEPS=0 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "Opção desconhecida: $arg" >&2; exit 1 ;;
  esac
done

info() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[aviso]\033[0m %s\n' "$*"; }

# privilégios para apt
SUDO=""; CAN_ROOT=0
if [[ $EUID -eq 0 ]]; then CAN_ROOT=1
elif command -v sudo >/dev/null 2>&1; then SUDO="sudo"; CAN_ROOT=1
fi
if [[ $INSTALL_DEPS -eq 1 && $CAN_ROOT -eq 0 ]]; then
  warn "sem root/sudo: pulando pacotes do sistema"; INSTALL_DEPS=0
fi
if [[ $INSTALL_DEPS -eq 1 ]] && ! command -v apt-get >/dev/null 2>&1; then
  warn "apt-get não encontrado (sistema não é Debian/Ubuntu): pulando pacotes do sistema"; INSTALL_DEPS=0
fi

cd "$APP_DIR"

if [[ $INSTALL_DEPS -eq 1 ]]; then
  info "Atualizando pacotes do sistema"
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -y
  $SUDO apt-get upgrade -y

  info "Instalando dependências base (python3, venv, pip, cron)"
  $SUDO apt-get install -y python3 python3-venv python3-pip cron
  $SUDO systemctl enable --now cron 2>/dev/null || $SUDO service cron start 2>/dev/null || true

  info "Instalando bibliotecas do Chromium/Playwright"
  # libasound2 virou libasound2t64 no Ubuntu 24.04+
  $SUDO apt-get install -y libnss3 libnspr4 libgbm1 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libpango-1.0-0 \
    libcairo2 fonts-liberation
  $SUDO apt-get install -y libasound2t64 2>/dev/null || $SUDO apt-get install -y libasound2 || true
fi

command -v python3 >/dev/null || { echo "python3 não encontrado" >&2; exit 1; }

info "Criando ambiente virtual e instalando dependências Python"
[[ -d venv ]] || python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

info "Instalando navegador Chromium do Playwright"
if [[ $EUID -eq 0 ]]; then
  playwright install chromium --with-deps
else
  # --with-deps exige root; as libs do sistema vão via sudo e o navegador
  # é baixado para o cache do usuário atual (o mesmo que roda o cron).
  if [[ $INSTALL_DEPS -eq 1 ]]; then
    $SUDO "$APP_DIR/venv/bin/playwright" install-deps chromium
  else
    warn "libs do sistema do Chromium não instaladas (--no-deps). Se falhar: sudo $APP_DIR/venv/bin/playwright install-deps chromium"
  fi
  playwright install chromium
fi

info "Preparando pastas e arquivos de configuração"
mkdir -p logs output
FIRST_RUN=0
if [[ ! -f .env ]]; then
  cp .env.example .env; FIRST_RUN=1
  warn ".env criado a partir do exemplo. EDITE com as credenciais reais: nano $APP_DIR/.env"
fi
if [[ ! -f clients.json ]]; then
  cp clients.example.json clients.json; FIRST_RUN=1
  warn "clients.json criado a partir do exemplo. EDITE com os clientes reais: nano $APP_DIR/clients.json"
fi
chmod 600 .env clients.json
chmod +x install.sh zabbix_report_agent.py

if [[ $INSTALL_CRON -eq 1 ]]; then
  info "Configurando crontab (todo domingo às 23:50)"
  command -v crontab >/dev/null || { echo "crontab não encontrado (instale o cron)" >&2; exit 1; }
  CRON_LINE="$CRON_SCHEDULE cd $APP_DIR && $APP_DIR/venv/bin/python $APP_DIR/zabbix_report_agent.py >> $APP_DIR/logs/cron.log 2>&1"
  # remove entrada anterior deste projeto e adiciona a atual (sem duplicar)
  ( crontab -l 2>/dev/null | grep -vF "$APP_DIR/zabbix_report_agent.py" || true; echo "$CRON_LINE" ) | crontab -
  crontab -l | grep -F "$APP_DIR/zabbix_report_agent.py"
fi

info "Instalação concluída"
if [[ $FIRST_RUN -eq 1 ]]; then
  echo "Próximos passos:"
  echo "  1) Edite as credenciais:  nano $APP_DIR/.env"
  echo "  2) Edite os clientes:     nano $APP_DIR/clients.json"
fi
echo "Teste manual:  $APP_DIR/venv/bin/python $APP_DIR/zabbix_report_agent.py"
echo "Logs:          $APP_DIR/logs/zabbix_report.log  (cron: $APP_DIR/logs/cron.log)"
