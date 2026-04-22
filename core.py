"""
Здесь лежит вся прикладная логика проекта:
- чтение .env,
- работа с PostgreSQL,
- auth и сессии,
- запросы к TheMealDB,
- запросы к Mistral.

app.py после этого остаётся коротким и отвечает в основном за маршруты.
"""

import hashlib
import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def load_env():
    """Простое чтение .env без дополнительных библиотек."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env()


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
PORT = int(os.getenv("PORT", "8000"))
DATABASE_URL = os.getenv("DATABASE_URL", "")
THEMEALDB_API_KEY = os.getenv("THEMEALDB_API_KEY", "1")
THEMEALDB_BASE_URL = f"https://www.themealdb.com/api/json/v1/{THEMEALDB_API_KEY}"
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

# По умолчанию оставляем обычную проверку сертификатов.
# Если в конкретной среде Python ругается на сертификаты, можно явно включить упрощённый режим.
SSL_CONTEXT = ssl._create_unverified_context() if os.getenv("ALLOW_INSECURE_SSL") == "1" else None


AI_TASKS = {
    "simplify": "Упрости рецепт для новичка. Напиши короткими шагами.",
    "shopping_list": "Сделай понятный список покупок по этому рецепту.",
    "substitutions": "Предложи замены ингредиентов и коротко поясни их.",
    "difficulty": "Оцени сложность по шкале от 1 до 5 и кратко объясни.",
    "student": "Скажи, подходит ли рецепт студенту и для быстрого приготовления.",
}


def read_template(name):
    """Читаем HTML-файл из templates."""
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def read_static(name):
    """Читаем файл из static."""
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def db():
    """На каждый запрос открываем новое подключение к PostgreSQL."""
    if not DATABASE_URL:
        raise RuntimeError("Добавьте DATABASE_URL в файл .env.")
    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)


def init_db():
    """Создаём только нужные для проекта таблицы."""
    with db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS recipes (
                id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id INTEGER NOT NULL,
                meal_id TEXT NOT NULL,
                title TEXT NOT NULL,
                category TEXT,
                area TEXT,
                instructions TEXT NOT NULL,
                image_url TEXT,
                youtube_url TEXT,
                ingredients_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(user_id, meal_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )


def password_hash(password):
    """Храним не сам пароль, а hash."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120_000,
    ).hex()
    return f"{salt}${digest}"


def password_ok(password, saved_hash):
    """Проверяем пароль пользователя."""
    salt, digest = saved_hash.split("$", 1)
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120_000,
    ).hex()
    return secrets.compare_digest(actual, digest)


def create_user(email, password):
    """Создаём пользователя и сразу возвращаем его id."""
    with db() as connection:
        if connection.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone():
            return None
        row = connection.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (%s, %s, %s) RETURNING id",
            (email, password_hash(password), int(time.time())),
        ).fetchone()
    return row["id"]


def find_user_by_email(email):
    """Ищем пользователя по email."""
    with db() as connection:
        row = connection.execute(
            "SELECT id, email, password_hash FROM users WHERE email = %s",
            (email,),
        ).fetchone()
    return row


def create_session(user_id):
    """Создаём серверную сессию."""
    token = secrets.token_urlsafe(32)
    with db() as connection:
        connection.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (%s, %s, %s)",
            (token, user_id, int(time.time())),
        )
    return token


def delete_session(token):
    """Удаляем сессию по токену."""
    if not token:
        return
    with db() as connection:
        connection.execute("DELETE FROM sessions WHERE token = %s", (token,))


def current_user(token):
    """Находим пользователя по session token."""
    if not token:
        return None

    with db() as connection:
        row = connection.execute(
            """
            SELECT users.id, users.email
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = %s
            """,
            (token,),
        ).fetchone()

    return dict(row) if row else None


def fetch_json(url, method="GET", payload=None, headers=None, timeout=30):
    """Общий helper для внешних HTTP-запросов."""
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers=headers or {"User-Agent": "RecipeApp/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        # На некоторых системах у Python бывает сломан доступ к корневым сертификатам.
        # В таком случае пробуем второй раз с упрощённой SSL-проверкой, чтобы проект работал.
        reason_text = str(getattr(error, "reason", error))
        if "CERTIFICATE_VERIFY_FAILED" in reason_text and SSL_CONTEXT is None:
            fallback_context = ssl._create_unverified_context()
            with urllib.request.urlopen(request, timeout=timeout, context=fallback_context) as response:
                return json.loads(response.read().decode("utf-8"))
        raise


def normalize_meal(meal):
    """Приводим ответ TheMealDB к удобному виду."""
    ingredients = []
    for index in range(1, 21):
        name = (meal.get(f"strIngredient{index}") or "").strip()
        measure = (meal.get(f"strMeasure{index}") or "").strip()
        if name:
            ingredients.append({"name": name, "measure": measure})

    return {
        "meal_id": meal.get("idMeal"),
        "title": meal.get("strMeal") or "",
        "category": meal.get("strCategory") or "",
        "area": meal.get("strArea") or "",
        "instructions": meal.get("strInstructions") or "",
        "image_url": meal.get("strMealThumb") or "",
        "youtube_url": meal.get("strYoutube") or "",
        "ingredients": ingredients,
    }


def search_meals(query):
    """Поиск рецептов по названию."""
    url = f"{THEMEALDB_BASE_URL}/search.php?s={urllib.parse.quote(query)}"
    payload = fetch_json(url)
    return [normalize_meal(meal) for meal in (payload.get("meals") or [])]


def meal_by_id(meal_id):
    """Полная карточка рецепта по meal_id."""
    url = f"{THEMEALDB_BASE_URL}/lookup.php?i={urllib.parse.quote(str(meal_id))}"
    payload = fetch_json(url)
    meals = payload.get("meals") or []
    return normalize_meal(meals[0]) if meals else None


def recipe_row(row):
    """Делаем из строки PostgreSQL обычный словарь."""
    return {
        "id": row["id"],
        "meal_id": row["meal_id"],
        "title": row["title"],
        "category": row["category"],
        "area": row["area"],
        "instructions": row["instructions"],
        "image_url": row["image_url"],
        "youtube_url": row["youtube_url"],
        "ingredients": json.loads(row["ingredients_json"]),
        "created_at": row["created_at"],
    }


def save_recipe(user_id, recipe):
    """Сохраняем рецепт в локальную базу."""
    with db() as connection:
        row = connection.execute(
            """
            INSERT INTO recipes (
                user_id, meal_id, title, category, area, instructions,
                image_url, youtube_url, ingredients_json, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(user_id, meal_id) DO UPDATE SET
                title = excluded.title,
                category = excluded.category,
                area = excluded.area,
                instructions = excluded.instructions,
                image_url = excluded.image_url,
                youtube_url = excluded.youtube_url,
                ingredients_json = excluded.ingredients_json
            RETURNING *
            """,
            (
                user_id,
                recipe["meal_id"],
                recipe["title"],
                recipe["category"],
                recipe["area"],
                recipe["instructions"],
                recipe["image_url"],
                recipe["youtube_url"],
                json.dumps(recipe["ingredients"], ensure_ascii=False),
                int(time.time()),
            ),
        ).fetchone()

    return recipe_row(row)


def user_recipes(user_id):
    """Все сохранённые рецепты пользователя."""
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM recipes WHERE user_id = %s ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    return [recipe_row(row) for row in rows]


def user_recipe(user_id, recipe_id):
    """Один конкретный рецепт пользователя."""
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM recipes WHERE user_id = %s AND id = %s",
            (user_id, recipe_id),
        ).fetchone()
    return recipe_row(row) if row else None


def recipe_text(recipe):
    """Готовим рецепт в текст для Mistral."""
    ingredients = "\n".join(
        f"- {item['measure']} {item['name']}".strip()
        for item in recipe["ingredients"]
    )
    return (
        f"Название: {recipe['title']}\n"
        f"Категория: {recipe['category'] or 'не указана'}\n"
        f"Кухня: {recipe['area'] or 'не указана'}\n"
        f"Ингредиенты:\n{ingredients}\n\n"
        f"Инструкция:\n{recipe['instructions']}"
    )


def ask_mistral(recipe, user_prompt):
    """Один общий вызов Mistral."""
    api_key = os.getenv("MISTRAL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Добавьте MISTRAL_API_KEY в файл .env.")

    payload = {
        "model": os.getenv("MISTRAL_MODEL", "mistral-small-latest"),
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты помощник по рецептам. Отвечай по-русски, просто и коротко. "
                    "Опирайся только на переданный рецепт."
                ),
            },
            {
                "role": "user",
                "content": f"{recipe_text(recipe)}\n\nВопрос или задача:\n{user_prompt}",
            },
        ],
    }

    try:
        result = fetch_json(
            MISTRAL_API_URL,
            method="POST",
            payload=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            timeout=60,
        )
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Mistral вернул ошибку {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Не удалось подключиться к Mistral: {error.reason}") from error

    choices = result.get("choices") or []
    if not choices:
        raise RuntimeError("Mistral не вернул ответ.")

    return choices[0]["message"]["content"].strip()
