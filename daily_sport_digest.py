from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv:
    load_dotenv()


DEFAULT_SOURCE_URL = "https://www.sport.es/es/autor/maria-leiva/"
DEFAULT_AUTHOR_NAME = "Maria Leiva"
DEFAULT_TIMEZONE = "Europe/Madrid"
DEFAULT_SEND_TIME = "07:30"


@dataclass
class Config:
    source_url: str
    author_name: str
    timezone: str
    send_time: str
    max_index_pages: int
    max_articles: int | None
    send_empty_email: bool
    state_file: Path
    email_from: str
    email_to: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    openai_api_key: str | None
    openai_model: str


@dataclass
class Article:
    url: str
    title: str
    author: str
    published_at: datetime | None
    modified_at: datetime | None
    effective_date: datetime
    body: str
    summary: str = ""
    updated_after_previous_email: bool = False
    previous_date: str | None = None


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "si", "on"}


def load_config() -> Config:
    max_articles_raw = os.getenv("MAX_ARTICLES", "").strip()
    max_articles = int(max_articles_raw) if max_articles_raw else None

    return Config(
        source_url=os.getenv("SOURCE_URL", DEFAULT_SOURCE_URL).strip(),
        author_name=os.getenv("AUTHOR_NAME", DEFAULT_AUTHOR_NAME).strip(),
        timezone=os.getenv("TIMEZONE", DEFAULT_TIMEZONE).strip(),
        send_time=os.getenv("SEND_TIME", DEFAULT_SEND_TIME).strip(),
        max_index_pages=int(os.getenv("MAX_INDEX_PAGES", "4")),
        max_articles=max_articles,
        send_empty_email=env_bool("SEND_EMPTY_EMAIL", True),
        state_file=Path(os.getenv("STATE_FILE", "sent_history.json")),
        email_from=os.getenv("EMAIL_FROM", os.getenv("GMAIL_USERNAME", "")).strip(),
        email_to=os.getenv("EMAIL_TO", os.getenv("GMAIL_USERNAME", "")).strip(),
        smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "465")),
        smtp_user=os.getenv("GMAIL_USERNAME", "").strip(),
        smtp_password=os.getenv("GMAIL_APP_PASSWORD", "").strip(),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip() or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
    )


def parse_send_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def delivery_window(now: datetime, send_at: time) -> tuple[datetime, datetime]:
    end = now.replace(hour=send_at.hour, minute=send_at.minute, second=0, microsecond=0)
    if now < end:
        end -= timedelta(days=1)
    return end - timedelta(days=1), end


def should_run_now(now: datetime, send_at: time) -> bool:
    scheduled = now.replace(hour=send_at.hour, minute=send_at.minute, second=0, microsecond=0)
    return scheduled <= now <= scheduled + timedelta(minutes=75)


def normalize_url(base_url: str, href: str) -> str:
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    parsed = parsed._replace(fragment="", query="")
    path = re.sub(r"/{2,}", "/", parsed.path)
    return urlunparse(parsed._replace(path=path))


def load_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"sent": {}}
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_history(path: Path, history: dict[str, Any]) -> None:
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.6",
        }
    )
    return session


def fetch(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def paginated_author_urls(source_url: str, max_pages: int) -> list[str]:
    urls = [source_url]
    clean = source_url.rstrip("/")
    for page in range(2, max_pages + 1):
        urls.append(f"{clean}/p{page}/")
    return urls


def is_probably_article_url(source_url: str, candidate_url: str) -> bool:
    source_host = urlparse(source_url).netloc
    parsed = urlparse(candidate_url)
    if parsed.netloc != source_host:
        return False

    skipped_fragments = (
        "/autor/",
        "/tag/",
        "/tags/",
        "/temas/",
        "/newsletter",
        "/servicios",
        "/club",
        "/hemeroteca",
        "/sitemap",
        "/buscar",
        "/login",
        "/registro",
        "/videos/",
    )
    if any(fragment in parsed.path for fragment in skipped_fragments):
        return False
    return parsed.path.startswith("/es/") and len(parsed.path.strip("/").split("/")) >= 3


def discover_article_links(session: requests.Session, config: Config) -> list[tuple[str, str]]:
    seen: set[str] = set()
    links: list[tuple[str, str]] = []

    for index_url in paginated_author_urls(config.source_url, config.max_index_pages):
        try:
            soup = BeautifulSoup(fetch(session, index_url), "html.parser")
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                break
            raise

        for anchor in soup.find_all("a", href=True):
            title = " ".join(anchor.get_text(" ", strip=True).split())
            if len(title) < 12:
                continue
            url = normalize_url(config.source_url, anchor["href"])
            if url in seen or not is_probably_article_url(config.source_url, url):
                continue
            seen.add(url)
            links.append((url, title))

    return links


def iter_jsonld_nodes(data: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(data, dict):
        nodes.append(data)
        graph = data.get("@graph")
        if isinstance(graph, list):
            nodes.extend(node for node in graph if isinstance(node, dict))
    elif isinstance(data, list):
        for item in data:
            nodes.extend(iter_jsonld_nodes(item))
    return nodes


def get_jsonld_articles(soup: BeautifulSoup) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in iter_jsonld_nodes(data):
            node_type = node.get("@type", "")
            if isinstance(node_type, list):
                types = {str(item).lower() for item in node_type}
            else:
                types = {str(node_type).lower()}
            if types & {"newsarticle", "article", "reportagenewsarticle"}:
                articles.append(node)
    return articles


def get_meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return None


def parse_datetime(value: Any, timezone_name: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(str(value), dayfirst=True)
    except (TypeError, ValueError, OverflowError):
        return None
    tz = ZoneInfo(timezone_name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def author_to_text(author: Any) -> str:
    if isinstance(author, str):
        return author
    if isinstance(author, dict):
        return str(author.get("name", ""))
    if isinstance(author, list):
        return ", ".join(author_to_text(item) for item in author)
    return ""


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "").replace("\xa0", " ")).strip()


def extract_article(session: requests.Session, url: str, index_title: str, config: Config) -> Article | None:
    soup = BeautifulSoup(fetch(session, url), "html.parser")
    jsonld_articles = get_jsonld_articles(soup)
    jsonld = jsonld_articles[0] if jsonld_articles else {}

    title_value = jsonld.get("headline") or get_meta(soup, "og:title", "twitter:title")
    if not title_value and soup.title and soup.title.string:
        title_value = soup.title.string
    title = compact_text(str(title_value or index_title))
    title = re.sub(r"\s*\|\s*SPORT\s*$", "", title, flags=re.IGNORECASE)

    author = compact_text(author_to_text(jsonld.get("author")) or get_meta(soup, "author", "article:author") or "")
    published_at = parse_datetime(
        jsonld.get("datePublished") or get_meta(soup, "article:published_time", "datePublished"),
        config.timezone,
    )
    modified_at = parse_datetime(
        jsonld.get("dateModified") or get_meta(soup, "article:modified_time", "dateModified"),
        config.timezone,
    )

    body = compact_text(str(jsonld.get("articleBody") or ""))
    if not body:
        article_tag = soup.find("article") or soup.find("main") or soup.body
        paragraphs = article_tag.find_all("p") if article_tag else []
        body = "\n".join(compact_text(paragraph.get_text(" ", strip=True)) for paragraph in paragraphs)
        body = "\n".join(line for line in body.splitlines() if len(line) > 40)

    effective = modified_at or published_at
    if not title or not effective or not body:
        return None

    return Article(
        url=url,
        title=title,
        author=author,
        published_at=published_at,
        modified_at=modified_at,
        effective_date=effective,
        body=body,
    )


def normalize_person_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def filter_articles(
    articles: list[Article],
    config: Config,
    history: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
) -> list[Article]:
    selected: list[Article] = []
    wanted_author = normalize_person_name(config.author_name)
    sent = history.setdefault("sent", {})

    for article in articles:
        if article.author and wanted_author not in normalize_person_name(article.author):
            continue

        published_in_window = article.published_at is not None and window_start <= article.published_at < window_end
        modified_in_window = article.modified_at is not None and window_start <= article.modified_at < window_end
        if not published_in_window and not modified_in_window:
            continue

        previous = sent.get(article.url)
        current_date = (article.modified_at or article.published_at or article.effective_date).isoformat()
        if previous:
            previous_date = previous.get("last_seen_date")
            if previous_date == current_date:
                continue
            article.updated_after_previous_email = True
            article.previous_date = previous_date

        article.effective_date = article.modified_at if modified_in_window else article.published_at or article.effective_date
        selected.append(article)

    selected.sort(key=lambda item: item.effective_date)
    if config.max_articles:
        selected = selected[: config.max_articles]
    return selected


def basic_summary(text: str) -> str:
    cleaned = compact_text(text)
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    picked = [sentence for sentence in sentences if len(sentence) > 40][:4]
    if not picked:
        return cleaned[:900]
    midpoint = min(2, len(picked))
    first = " ".join(picked[:midpoint])
    second = " ".join(picked[midpoint:])
    return first if not second else f"{first}\n\n{second}"


def summarize_with_openai(article: Article, config: Config) -> str:
    if not config.openai_api_key:
        return basic_summary(article.body)

    from openai import OpenAI

    client = OpenAI(api_key=config.openai_api_key)
    prompt = (
        "Resume esta noticia en espanol para un correo diario. "
        "Devuelve exactamente dos parrafos, con 3-4 lineas en total. "
        "Se conciso, conserva los datos importantes y no inventes informacion.\n\n"
        f"TITULAR: {article.title}\n\n"
        f"TEXTO:\n{article.body[:7000]}"
    )
    response = client.chat.completions.create(
        model=config.openai_model,
        messages=[
            {"role": "system", "content": "Eres un redactor de resumenes deportivos en espanol."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    return (response.choices[0].message.content or "").strip()


def add_summaries(articles: list[Article], config: Config) -> None:
    for article in articles:
        article.summary = summarize_with_openai(article, config)


def format_dt(value: datetime | None) -> str:
    if not value:
        return "Sin fecha"
    return value.strftime("%d/%m/%Y %H:%M")


def windows_safe_subject(window_end: datetime) -> str:
    return f"Resumen diario de noticias - {window_end.day}/{window_end.month}/{window_end.year}"


def build_plain_email(articles: list[Article], window_start: datetime, window_end: datetime) -> str:
    header = [
        f"Resumen de noticias de Maria Leiva",
        f"Periodo: {format_dt(window_start)} - {format_dt(window_end)}",
        f"Noticias encontradas: {len(articles)}",
        "",
    ]
    if not articles:
        header.append("No se han encontrado noticias nuevas en este periodo.")
        return "\n".join(header)

    blocks = []
    for index, article in enumerate(articles, start=1):
        updated_note = ""
        if article.updated_after_previous_email:
            updated_note = "\nAviso: esta noticia ya habia aparecido antes, pero ahora tiene una fecha nueva."
        blocks.append(
            "\n".join(
                [
                    f"{index}. {article.title}",
                    f"Fecha: {format_dt(article.effective_date)}",
                    f"Enlace: {article.url}",
                    updated_note.strip(),
                    "",
                    article.summary,
                ]
            ).strip()
        )
    return "\n".join(header) + "\n\n---\n\n" + "\n\n---\n\n".join(blocks)


def build_html_email(articles: list[Article], window_start: datetime, window_end: datetime) -> str:
    css = (
        "font-family: Arial, sans-serif; color: #1f2933; line-height: 1.45; "
        "max-width: 760px; margin: 0 auto;"
    )
    pieces = [
        f'<div style="{css}">',
        "<h1 style=\"font-size:24px; margin-bottom:6px;\">Resumen de noticias de Maria Leiva</h1>",
        f"<p><strong>Periodo:</strong> {html.escape(format_dt(window_start))} - {html.escape(format_dt(window_end))}</p>",
        f"<p><strong>Noticias encontradas:</strong> {len(articles)}</p>",
    ]

    if not articles:
        pieces.append("<p>No se han encontrado noticias nuevas en este periodo.</p>")
    else:
        for article in articles:
            pieces.append('<hr style="border:0; border-top:1px solid #d9e2ec; margin:24px 0;">')
            pieces.append(f"<h2 style=\"font-size:20px; margin-bottom:8px;\">{html.escape(article.title)}</h2>")
            pieces.append(f"<p><strong>Fecha:</strong> {html.escape(format_dt(article.effective_date))}</p>")
            pieces.append(f'<p><strong>Enlace:</strong> <a href="{html.escape(article.url)}">{html.escape(article.url)}</a></p>')
            if article.updated_after_previous_email:
                pieces.append(
                    "<p style=\"background:#fff7ed; border-left:4px solid #f97316; padding:10px;\">"
                    "Aviso: esta noticia ya habia aparecido antes, pero ahora tiene una fecha nueva."
                    "</p>"
                )
            summary_html = "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in article.summary.split("\n\n"))
            pieces.append(summary_html)

    pieces.append("</div>")
    return "\n".join(pieces)


def send_email(config: Config, subject: str, plain_body: str, html_body: str | None = None) -> None:
    missing = [
        name
        for name, value in {
            "GMAIL_USERNAME": config.smtp_user,
            "GMAIL_APP_PASSWORD": config.smtp_password,
            "EMAIL_FROM": config.email_from,
            "EMAIL_TO": config.email_to,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required email settings: {', '.join(missing)}")

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = config.email_from
    message["To"] = config.email_to
    message.attach(MIMEText(plain_body, "plain", "utf-8"))
    if html_body:
        message.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port) as smtp:
        smtp.login(config.smtp_user, config.smtp_password)
        smtp.sendmail(config.email_from, [config.email_to], message.as_string())


def mark_sent(history: dict[str, Any], articles: list[Article], sent_at: datetime) -> None:
    sent = history.setdefault("sent", {})
    for article in articles:
        current_date = (article.modified_at or article.published_at or article.effective_date).isoformat()
        existing = sent.get(article.url, {})
        sent[article.url] = {
            "title": article.title,
            "first_sent_at": existing.get("first_sent_at", sent_at.isoformat()),
            "last_sent_at": sent_at.isoformat(),
            "last_seen_date": current_date,
        }


def run_digest(config: Config, dry_run: bool, only_if_delivery_window: bool) -> int:
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    send_at = parse_send_time(config.send_time)

    if only_if_delivery_window and not should_run_now(now, send_at):
        print(f"Skipping: local time {format_dt(now)} is outside the delivery window.")
        return 0

    window_start, window_end = delivery_window(now, send_at)
    history = load_history(config.state_file)
    session = make_session()

    print(f"Source: {config.source_url}")
    print(f"Window: {window_start.isoformat()} - {window_end.isoformat()}")

    candidates = discover_article_links(session, config)
    print(f"Candidate links: {len(candidates)}")

    extracted: list[Article] = []
    for url, title in candidates:
        try:
            article = extract_article(session, url, title, config)
        except Exception as exc:
            print(f"Could not extract {url}: {exc}", file=sys.stderr)
            continue
        if article:
            extracted.append(article)

    selected = filter_articles(extracted, config, history, window_start, window_end)
    print(f"Selected articles: {len(selected)}")

    if not selected and not config.send_empty_email:
        print("No articles and SEND_EMPTY_EMAIL=false. Nothing to send.")
        return 0

    add_summaries(selected, config)

    subject = windows_safe_subject(window_end)
    plain_body = build_plain_email(selected, window_start, window_end)
    html_body = build_html_email(selected, window_start, window_end)

    if dry_run:
        print("\n--- EMAIL SUBJECT ---")
        print(subject)
        print("\n--- EMAIL BODY ---")
        print(plain_body)
        return 0

    send_email(config, subject, plain_body, html_body)
    mark_sent(history, selected, datetime.now(tz))
    save_history(config.state_file, history)
    print("Email sent and history updated.")
    return 0


def send_failure_email(config: Config, error: BaseException) -> None:
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    subject = f"Fallo resumen diario de noticias - {now.day}/{now.month}/{now.year}"
    details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    body = (
        "Ha fallado la automatizacion del resumen diario.\n\n"
        f"Fecha: {format_dt(now)}\n"
        f"Fuente: {config.source_url}\n\n"
        f"Detalle tecnico:\n{details[-6000:]}"
    )
    send_email(config, subject, body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a daily Sport.es author news digest by email.")
    parser.add_argument("--dry-run", action="store_true", help="Build the digest but do not send email or update history.")
    parser.add_argument(
        "--only-if-delivery-window",
        action="store_true",
        help="Skip unless current local time is within the configured delivery window.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config()
    try:
        return run_digest(config, dry_run=args.dry_run, only_if_delivery_window=args.only_if_delivery_window)
    except Exception as exc:
        print(f"Digest failed: {exc}", file=sys.stderr)
        if not args.dry_run:
            try:
                send_failure_email(config, exc)
                print("Failure email sent.", file=sys.stderr)
            except Exception as email_exc:
                print(f"Could not send failure email: {email_exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
