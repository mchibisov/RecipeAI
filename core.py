import hashlib
import os
import secrets
import time
from pathlib import Path

import requests
from sqlalchemy import ForeignKey, Identity, Integer, Text, create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def load_env():
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env()

PORT = int(os.getenv("PORT", "8000"))
DATABASE_URL = os.getenv("DATABASE_URL", "")
THEMEALDB_URL = f"https://www.themealdb.com/api/json/v1/{os.getenv('THEMEALDB_API_KEY', '1')}"
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"


def sqlalchemy_url():
    if not DATABASE_URL:
        raise RuntimeError("Add DATABASE_URL to .env.")
    if DATABASE_URL.startswith("postgresql://"):
        return DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    return DATABASE_URL


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class UserSession(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


engine = create_engine(sqlalchemy_url())
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(engine)


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 120_000
    ).hex()
    return f"{salt}${digest}"


def password_ok(password, saved_hash):
    salt, old_digest = saved_hash.split("$", 1)
    new_digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 120_000
    ).hex()
    return secrets.compare_digest(new_digest, old_digest)


def user_by_email(email):
    with SessionLocal() as session:
        user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()

    if not user:
        return None
    return {"id": user.id, "email": user.email, "password_hash": user.password_hash}


def create_user(email, password):
    try:
        with SessionLocal.begin() as session:
            user = User(
                email=email,
                password_hash=password_hash(password),
                created_at=int(time.time()),
            )
            session.add(user)
            session.flush()
            return user.id
    except IntegrityError:
        return None


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    with SessionLocal.begin() as session:
        session.add(
            UserSession(
                token=token,
                user_id=user_id,
                created_at=int(time.time()),
            )
        )
    return token


def delete_session(token):
    if token:
        with SessionLocal.begin() as session:
            session.execute(delete(UserSession).where(UserSession.token == token))


def current_user(token):
    if not token:
        return None

    with SessionLocal() as session:
        user = session.execute(
            select(User)
            .join(UserSession, User.id == UserSession.user_id)
            .where(UserSession.token == token)
        ).scalar_one_or_none()

    return {"id": user.id, "email": user.email} if user else None


def normalize_meal(meal):
    ingredients = []
    for number in range(1, 21):
        name = (meal.get(f"strIngredient{number}") or "").strip()
        measure = (meal.get(f"strMeasure{number}") or "").strip()
        if name:
            ingredients.append({"name": name, "measure": measure})

    return {
        "meal_id": meal.get("idMeal"),
        "title": meal.get("strMeal") or "",
        "category": meal.get("strCategory") or "",
        "area": meal.get("strArea") or "",
        "instructions": meal.get("strInstructions") or "",
        "image_url": meal.get("strMealThumb") or "",
        "ingredients": ingredients,
    }


def search_meals(query):
    response = requests.get(f"{THEMEALDB_URL}/search.php", params={"s": query}, timeout=20)
    response.raise_for_status()
    meals = response.json().get("meals") or []
    return [normalize_meal(meal) for meal in meals]


def recipe_text(recipe):
    ingredients = "\n".join(
        f"- {item.get('measure', '')} {item.get('name', '')}".strip()
        for item in recipe.get("ingredients", [])
    )
    return (
        f"Title: {recipe.get('title', '')}\n"
        f"Category: {recipe.get('category', '')}\n"
        f"Cuisine: {recipe.get('area', '')}\n"
        f"Ingredients:\n{ingredients}\n\n"
        f"Instructions:\n{recipe.get('instructions', '')}"
    )


def ask_mistral(recipe, question):
    api_key = os.getenv("MISTRAL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Add MISTRAL_API_KEY to .env.")

    response = requests.post(
        MISTRAL_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": os.getenv("MISTRAL_MODEL", "mistral-small-latest"),
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": "You answer questions about the provided recipe briefly and clearly.",
                },
                {
                    "role": "user",
                    "content": f"{recipe_text(recipe)}\n\nQuestion: {question}",
                },
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()
