#!/usr/bin/env python3
"""Gera e envia relatórios PDF do Zabbix para múltiplos clientes.

- Credenciais e parâmetros globais: arquivo .env
- Clientes (nome, dashboards/hosts do Zabbix, e-mail de destino): clients.json

Uso:
    python zabbix_report_agent.py              # todos os clientes
    python zabbix_report_agent.py atlasnet     # só os clientes indicados (slug)
"""
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from dotenv import load_dotenv
from fpdf import FPDF
from PIL import Image
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("zabbix_report")


# --------------------------------------------------------------------------- #
# Configuração global (.env)
# --------------------------------------------------------------------------- #
def _req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente no .env: {name}")
    return value


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "sim")


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else BASE_DIR / p


@dataclass
class Config:
    base_url: str
    user: str
    password: str
    verify_tls: bool
    time_from: str
    time_to: str
    extra_wait: float
    vp_w: int
    vp_h: int
    kiosk: bool
    charts_per_page: int
    charts_max: int
    charts_name_filter: str
    title_template: str
    output_dir: Path
    retention_days: int
    clients_file: Path
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_verify_tls: bool
    smtp_user: str
    smtp_password: str
    mail_from: str
    mail_override: list[str]
    subject_template: str


def load_config() -> Config:
    load_dotenv(BASE_DIR / ".env")
    return Config(
        base_url=_req("ZABBIX_BASE_URL").rstrip("/"),
        user=_req("ZABBIX_USER"),
        password=_req("ZABBIX_PASSWORD"),
        verify_tls=_bool("ZABBIX_VERIFY_TLS", True),
        time_from=os.getenv("ZABBIX_TIME_FROM", "now-7d"),
        time_to=os.getenv("ZABBIX_TIME_TO", "now"),
        extra_wait=float(os.getenv("ZABBIX_EXTRA_WAIT_SECONDS", "8")),
        vp_w=int(os.getenv("ZABBIX_VIEWPORT_WIDTH", "1920")),
        vp_h=int(os.getenv("ZABBIX_VIEWPORT_HEIGHT", "1080")),
        kiosk=_bool("ZABBIX_KIOSK", False),
        charts_per_page=max(1, int(os.getenv("ZABBIX_CHARTS_PER_PAGE", "3"))),
        charts_max=int(os.getenv("ZABBIX_CHARTS_MAX", "0")),
        charts_name_filter=os.getenv("ZABBIX_CHARTS_NAME_FILTER", "").strip(),
        title_template=os.getenv("REPORT_TITLE", "Relatório Semanal de Monitoramento - {cliente}"),
        output_dir=_path(os.getenv("REPORT_OUTPUT_DIR", "./output")),
        retention_days=int(os.getenv("REPORT_RETENTION_DAYS", "90")),
        clients_file=_path(os.getenv("CLIENTS_FILE", "clients.json")),
        smtp_host=_req("SMTP_HOST"),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_security=os.getenv("SMTP_SECURITY", "starttls").strip().lower(),
        smtp_verify_tls=_bool("SMTP_VERIFY_TLS", True),
        smtp_user=os.getenv("SMTP_USER", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        mail_from=_req("MAIL_FROM"),
        mail_override=_split(os.getenv("MAIL_TO_OVERRIDE", "")),
        subject_template=os.getenv("MAIL_SUBJECT", "Relatório semanal Zabbix - {cliente}"),
    )


def _split(value) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


# --------------------------------------------------------------------------- #
# Clientes (clients.json)
# --------------------------------------------------------------------------- #
@dataclass
class Target:
    id: str
    title: str


@dataclass
class Client:
    name: str
    slug: str
    mail_to: list[str]
    mail_cc: list[str] = field(default_factory=list)
    subject: str = ""
    dashboards: list[Target] = field(default_factory=list)
    chart_hosts: list[Target] = field(default_factory=list)


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "cliente"


def _parse_target(item, kind: str, client: str) -> Target:
    """Aceita {"url": ...}, {"id": ...} (com "title"/"name" opcional) ou string (URL ou ID)."""
    if not isinstance(item, dict):
        item = {"url": item} if str(item).startswith("http") else {"id": item}
    title = str(item.get("title") or item.get("name") or "").strip()
    target_id = str(item.get("id") or "").strip()
    url = str(item.get("url") or "").strip()
    if url and not target_id:
        qs = parse_qs(urlparse(url).query)
        key = "dashboardid" if kind == "dashboard" else "filter_hostids[0]"
        values = qs.get(key) or qs.get("filter_hostids%5B0%5D") or []
        if not values:
            raise ValueError(f"cliente '{client}': não achei {key} na URL: {url}")
        target_id = values[0]
    if not target_id.isdigit():
        raise ValueError(f"cliente '{client}': id de {kind} inválido: {item!r}")
    default = f"Dashboard {target_id}" if kind == "dashboard" else f"Host {target_id}"
    return Target(target_id, title or default)


def load_clients(path: Path) -> list[Client]:
    if not path.exists():
        raise RuntimeError(f"Arquivo de clientes não encontrado: {path} (veja clients.example.json)")
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data["clients"] if isinstance(data, dict) else data
    clients: list[Client] = []
    seen: set[str] = set()
    for entry in raw:
        if entry.get("enabled", True) is False:
            continue
        name = str(entry["name"]).strip()
        slug = _slugify(entry.get("slug") or name)
        if slug in seen:
            raise ValueError(f"slug duplicado em clients.json: {slug}")
        seen.add(slug)
        mail_to = _split(entry.get("mail_to"))
        if not mail_to:
            raise ValueError(f"cliente '{name}': mail_to vazio")
        c = Client(
            name=name,
            slug=slug,
            mail_to=mail_to,
            mail_cc=_split(entry.get("mail_cc")),
            subject=str(entry.get("subject") or "").strip(),
            dashboards=[_parse_target(d, "dashboard", name) for d in entry.get("dashboards", [])],
            chart_hosts=[_parse_target(h, "host", name) for h in entry.get("chart_hosts", [])],
        )
        if not c.dashboards and not c.chart_hosts:
            raise ValueError(f"cliente '{name}': defina dashboards e/ou chart_hosts")
        clients.append(c)
    if not clients:
        raise RuntimeError("Nenhum cliente habilitado em clients.json")
    return clients


# --------------------------------------------------------------------------- #
# Captura (Playwright)
# --------------------------------------------------------------------------- #
def _login(page, cfg: Config) -> None:
    page.goto(f"{cfg.base_url}/index.php", wait_until="domcontentloaded")
    page.fill("#name", cfg.user)
    page.fill("#password", cfg.password)
    page.click("#enter")
    page.wait_for_load_state("networkidle")
    # Se o formulário de login ainda existe, a autenticação falhou
    if page.locator("#name").count() > 0 and page.locator("#enter").count() > 0:
        raise RuntimeError("Falha no login do Zabbix (verifique usuário/senha no .env)")
    log.info("Login no Zabbix OK")


def _scroll_to_load(page) -> None:
    """Rola a página para disparar widgets carregados sob demanda."""
    height = page.evaluate("document.documentElement.scrollHeight")
    y = 0
    while y < height:
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(400)
        y += 600
        height = page.evaluate("document.documentElement.scrollHeight")
    page.evaluate("window.scrollTo(0, 0)")


def _wait_render(page, cfg: Config, label: str) -> None:
    page.wait_for_load_state("networkidle")
    _scroll_to_load(page)
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PWTimeout:
        log.warning("networkidle expirou em %s; seguindo com a pausa extra", label)
    page.wait_for_timeout(int(cfg.extra_wait * 1000))
    # Zabbix mostra .msg-bad quando o ID não existe ou falta permissão
    if page.locator(".msg-bad").count() > 0:
        err = page.locator(".msg-bad").first.inner_text().strip().replace("\n", " ")
        raise RuntimeError(f"'{label}' retornou erro do Zabbix: {err}")


def _capture_dashboard(page, cfg: Config, target: Target, tmp: Path) -> Path:
    url = (
        f"{cfg.base_url}/zabbix.php?action=dashboard.view"
        f"&dashboardid={target.id}&from={cfg.time_from}&to={cfg.time_to}"
    )
    if cfg.kiosk:
        url += "&kiosk=1"
    log.info("Capturando dashboard '%s' (%s)", target.title, target.id)
    page.goto(url, wait_until="domcontentloaded")
    _wait_render(page, cfg, f"dashboard {target.id} ({target.title})")
    path = tmp / f"dash_{target.id}.png"
    page.screenshot(path=str(path), full_page=True)
    return path


def _capture_host_charts(page, cfg: Config, target: Target, tmp: Path) -> list[Path]:
    """Salva cada gráfico da página 'Gráficos' do host como uma imagem separada."""
    url = (
        f"{cfg.base_url}/zabbix.php?action=charts.view&filter_hostids%5B0%5D={target.id}"
        f"&filter_show=1&filter_set=1&from={cfg.time_from}&to={cfg.time_to}"
    )
    if cfg.charts_name_filter:
        url += f"&filter_name={quote(cfg.charts_name_filter)}"
    log.info("Capturando gráficos do host '%s' (%s)", target.title, target.id)
    page.goto(url, wait_until="domcontentloaded")
    _wait_render(page, cfg, f"gráficos {target.title}")
    imgs = page.locator(".flickerfreescreen img")
    total = imgs.count()
    if cfg.charts_max > 0:
        total = min(total, cfg.charts_max)
    paths: list[Path] = []
    for i in range(total):
        img = imgs.nth(i)
        img.scroll_into_view_if_needed()
        # espera a imagem do gráfico terminar de carregar
        img.evaluate(
            "el => el.complete && el.naturalWidth > 0 ? true : new Promise(r => "
            "{ el.onload = () => r(true); setTimeout(() => r(false), 20000); })"
        )
        path = tmp / f"chart_{target.id}_{i:03d}.png"
        img.screenshot(path=str(path))
        paths.append(path)
    if not paths:
        raise RuntimeError(f"Nenhum gráfico encontrado para o host {target.id} ('{target.title}')")
    log.info("%d gráficos capturados para '%s'", len(paths), target.title)
    return paths


def capture_client(context, cfg: Config, client: Client, tmp: Path):
    """Captura tudo de um cliente numa aba própria (uma aba com problema não afeta as outras)."""
    page = context.new_page()
    page.set_default_timeout(60_000)
    try:
        shots = [(t.title, _capture_dashboard(page, cfg, t, tmp)) for t in client.dashboards]
        groups = [
            (f"Gráficos do agent - {t.title}", _capture_host_charts(page, cfg, t, tmp))
            for t in client.chart_hosts
        ]
        return shots, groups
    finally:
        page.close()


# --------------------------------------------------------------------------- #
# PDF (fpdf2)
# --------------------------------------------------------------------------- #
def _latin1(text: str) -> str:
    """Fontes core do PDF só suportam latin-1; troca o que não couber por '?'."""
    return text.encode("latin-1", "replace").decode("latin-1")


def build_pdf(
    cfg: Config,
    title: str,
    shots: list[tuple[str, Path]],
    chart_groups: list[tuple[str, list[Path]]],
    out_path: Path,
    tmp: Path,
) -> None:
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(False)
    margin, header_h = 8, 14
    page_w, page_h = pdf.w, pdf.h
    usable_w = page_w - 2 * margin
    usable_h = page_h - margin - header_h - 6
    generated = datetime.now().strftime("%d/%m/%Y %H:%M")
    period = f"{cfg.time_from} a {cfg.time_to}"

    def new_page(heading: str) -> None:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_xy(margin, margin)
        pdf.cell(usable_w, 8, _latin1(heading), new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(180, 180, 180)
        pdf.line(margin, margin + 9, page_w - margin, margin + 9)

    def footer() -> None:
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_xy(margin, page_h - margin)
        pdf.cell(usable_w, 4, _latin1(f"{title} - {generated} - pág. {pdf.page_no()}"), align="R")

    # Capa
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 28)
    pdf.set_y(page_h / 2 - 25)
    pdf.multi_cell(0, 14, _latin1(title), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 14)
    pdf.cell(0, 10, _latin1(f"Período: {period}"), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 10, _latin1(f"Gerado em {generated}"), align="C", new_x="LMARGIN", new_y="NEXT")

    # Dashboards (captura de página inteira, fatiada em páginas se for alta)
    for name, path in shots:
        with Image.open(path) as img:
            img = img.convert("RGB")
            w, h = img.size
            slice_px = max(1, int(usable_h * w / usable_w))
            parts = [(top, min(top + slice_px, h)) for top in range(0, h, slice_px)]
            for i, (top, bottom) in enumerate(parts, 1):
                piece = tmp / f"{path.stem}_{i}.jpg"
                img.crop((0, top, w, bottom)).save(piece, quality=88)
                suffix = f" ({i}/{len(parts)})" if len(parts) > 1 else ""
                new_page(f"{name}{suffix}")
                pdf.image(
                    str(piece), x=margin, y=margin + header_h - 2, w=usable_w,
                    h=(bottom - top) * usable_w / w,
                )
                footer()

    # Gráficos individuais: N por página, empilhados
    n = cfg.charts_per_page
    gap = 4
    slot_h = (usable_h - gap * (n - 1)) / n
    for group_title, paths in chart_groups:
        pages = [paths[i : i + n] for i in range(0, len(paths), n)]
        for pi, group in enumerate(pages, 1):
            new_page(f"{group_title} ({pi}/{len(pages)})")
            y = margin + header_h - 2
            for path in group:
                with Image.open(path) as im:
                    w, h = im.size
                draw_w = min(usable_w, slot_h * w / h)
                draw_h = draw_w * h / w
                pdf.image(str(path), x=margin + (usable_w - draw_w) / 2, y=y, w=draw_w, h=draw_h)
                y += slot_h + gap
            footer()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    log.info("PDF gerado: %s", out_path)


# --------------------------------------------------------------------------- #
# E-mail (smtplib)
# --------------------------------------------------------------------------- #
def send_email(cfg: Config, client: Client, pdf_path: Path) -> list[str]:
    to = cfg.mail_override or client.mail_to
    cc = [] if cfg.mail_override else client.mail_cc
    subject = (client.subject or cfg.subject_template).replace("{cliente}", client.name)

    msg = MIMEMultipart()
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    body = (
        f"Prezados da {client.name},\n\n"
        f"Segue em anexo o relatório de monitoramento referente ao período "
        f"{cfg.time_from} a {cfg.time_to}.\n\nAtenciosamente,\nNOC"
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))
    part = MIMEApplication(pdf_path.read_bytes(), _subtype="pdf")
    part.add_header("Content-Disposition", "attachment", filename=pdf_path.name)
    msg.attach(part)

    recipients = to + cc
    ctx = ssl.create_default_context()
    if not cfg.smtp_verify_tls:
        log.warning("SMTP_VERIFY_TLS=false: certificado do servidor SMTP NÃO está sendo validado")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if cfg.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx, timeout=60)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60)
    with server:
        if cfg.smtp_security == "starttls":
            server.starttls(context=ctx)
        if cfg.smtp_user:
            server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg, from_addr=cfg.mail_from, to_addrs=recipients)
    log.info(
        "ENVIO OK | cliente=%s | assunto=%r | anexo=%s | destinatários=%s",
        client.name, subject, pdf_path.name, ", ".join(recipients),
    )
    return recipients


# --------------------------------------------------------------------------- #
# Execução
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    """Log no console e em arquivo rotativo (LOG_FILE, padrão logs/zabbix_report.log)."""
    load_dotenv(BASE_DIR / ".env")
    log_file = _path(os.getenv("LOG_FILE", "logs/zabbix_report.log"))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=10, encoding="utf-8"),
    ]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)


def cleanup_old_reports(output_dir: Path, keep_days: int) -> None:
    """Remove PDFs (e pastas de data vazias) mais antigos que keep_days."""
    if keep_days <= 0:
        return
    limit = datetime.now().timestamp() - keep_days * 86400
    for f in output_dir.glob("*/relatorio_*.pdf"):
        try:
            if f.stat().st_mtime < limit:
                f.unlink()
                log.info("PDF antigo removido: %s/%s", f.parent.name, f.name)
        except OSError as e:
            log.warning("Não foi possível remover %s: %s", f, e)
    for d in output_dir.iterdir():
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


def process_client(context, cfg: Config, client: Client, run_dir: Path) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        shots, groups = capture_client(context, cfg, client, tmp)
        title = cfg.title_template.replace("{cliente}", client.name)
        pdf_path = run_dir / f"relatorio_{client.slug}.pdf"
        build_pdf(cfg, title, shots, groups, pdf_path, tmp)
    send_email(cfg, client, pdf_path)


def main(argv: list[str]) -> int:
    setup_logging()
    log.info("=== Início da execução do relatório ===")
    try:
        cfg = load_config()
        clients = load_clients(cfg.clients_file)
        if argv:
            wanted = {_slugify(a) for a in argv}
            unknown = wanted - {c.slug for c in clients}
            if unknown:
                raise RuntimeError(f"Cliente(s) não encontrado(s) em clients.json: {', '.join(sorted(unknown))}")
            clients = [c for c in clients if c.slug in wanted]
        if cfg.mail_override:
            log.warning("MAIL_TO_OVERRIDE ativo: todos os e-mails vão para %s", ", ".join(cfg.mail_override))
    except Exception:
        log.exception("FALHA ao carregar a configuração - nada foi executado")
        return 1

    run_dir = cfg.output_dir / datetime.now().strftime("%Y-%m-%d")
    ok: list[str] = []
    failed: list[str] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": cfg.vp_w, "height": cfg.vp_h},
                ignore_https_errors=not cfg.verify_tls,
                locale="pt-BR",
            )
            try:
                login_page = context.new_page()
                login_page.set_default_timeout(60_000)
                _login(login_page, cfg)
                login_page.close()
                for client in clients:
                    log.info("--- Cliente: %s ---", client.name)
                    try:
                        process_client(context, cfg, client, run_dir)
                        ok.append(client.name)
                    except Exception:
                        log.exception("FALHA no cliente %s - seguindo para o próximo", client.name)
                        failed.append(client.name)
            finally:
                context.close()
                browser.close()
    except Exception:
        log.exception("FALHA geral (login/navegador) - clientes não processados")
        failed.extend(c.name for c in clients if c.name not in ok and c.name not in failed)

    try:
        cleanup_old_reports(cfg.output_dir, cfg.retention_days)
    except Exception:
        log.exception("Falha na limpeza de PDFs antigos (ignorada)")

    log.info("=== Resumo: %d OK (%s) | %d falha(s) (%s) ===",
             len(ok), ", ".join(ok) or "-", len(failed), ", ".join(failed) or "-")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
