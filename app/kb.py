import json
import re
from pathlib import Path

from sqlalchemy import delete, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import KbArticle

ARTICLES_PATH = Path(__file__).parent / "knowledge_base" / "articles.json"

TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
MIN_TOKEN_LENGTH = 3
MAX_QUERY_TERMS = 30

STOPWORDS = {
    "або", "але", "без", "будь", "вам", "вас", "вже", "від", "вітаю", "для", "добрий", "день",
    "допоможіть", "дякую", "зараз", "коли", "ласка", "мене", "мені", "мій", "мої", "моє",
    "можу", "можна", "над", "нам", "нас", "немає", "нема", "під", "підкажіть", "після", "привіт",
    "при", "проблема", "питання", "так", "також", "там", "тому", "треба", "хочу", "чому", "через",
    "що", "щоб", "який", "яка", "яке", "які", "як", "the", "and", "for", "with", "not", "can",
    "how", "what", "you", "your", "this", "that", "are", "please", "hello",
}


def load_articles() -> list[dict]:
    return json.loads(ARTICLES_PATH.read_text(encoding="utf-8"))


def article_url(slug: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/kb/{slug}"


def article_ref(article: KbArticle) -> dict:
    return {"slug": article.slug, "title": article.title, "url": article_url(article.slug)}


def _stem(token: str) -> str:
    return token[: min(5, max(4, len(token) - 2))]


def build_tsquery(text: str) -> str | None:
    terms: list[str] = []
    for token in TOKEN_RE.findall(text.lower()):
        if len(token) < MIN_TOKEN_LENGTH or token in STOPWORDS:
            continue
        term = f"{_stem(token)}:*"
        if term not in terms:
            terms.append(term)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    return " | ".join(terms) if terms else None


async def search_articles(session: AsyncSession, text: str, limit: int) -> list[KbArticle]:
    query = build_tsquery(text)
    if not query:
        return []
    ts_query = func.to_tsquery(literal_column("'simple'::regconfig"), query)
    rank = func.ts_rank(KbArticle.search_vector, ts_query)
    stmt = (
        select(KbArticle)
        .where(KbArticle.search_vector.op("@@")(ts_query))
        .order_by(rank.desc(), KbArticle.id)
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def seed_knowledge_base(session: AsyncSession) -> int:
    articles = load_articles()
    for article in articles:
        stmt = pg_insert(KbArticle).values(
            slug=article["slug"],
            title=article["title"],
            keywords=article.get("keywords", ""),
            body=article["body"],
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[KbArticle.slug],
            set_={
                "title": stmt.excluded.title,
                "keywords": stmt.excluded.keywords,
                "body": stmt.excluded.body,
            },
        )
        await session.execute(stmt)
    await session.execute(delete(KbArticle).where(KbArticle.slug.not_in([a["slug"] for a in articles])))
    await session.commit()
    return len(articles)
