import hashlib  # встроенная библиотека Python для создания хешей: sha256, md5, sha1 и др. Это нужно, чтобы в базе данных не лежал настоящий пароль пользователя. При входе я хеширую введённый пароль и сравниваю полученный хеш с тем, что хранится в базе.
import os  # модуль для взаимодействия с операционной системой. os.getenv("DATABASE_URL")
import secrets  # модуль для безопасной генерации случайных строк. для генерации безопасного случайного токена сессии, который сложно угадать.
import time  # time — модуль для работы со временем.time нужен, чтобы считать срок действия сессии. Например, сессия живёт определённое количество секунд, потом становится недействительной.

import requests  # HTTP-клиент для запросов к внешним API
from dotenv import load_dotenv
from sqlalchemy import (
    ForeignKey,  # задаёт внешний ключ между таблицами
    Identity,  # задаёт автоматическую генерацию значения, обычно для id.
    Integer,  # SQL-тип целого числа
    Text,
    create_engine,  # создаёт объект подключения SQLAlchemy к базе данных.
    delete,  # строит SQL-запрос DELETE.
    select,  # строит SQL-запрос SELECT.
)
from sqlalchemy.exc import (
    IntegrityError,
)  # ошибка SQLAlchemy при нарушении ограничений БД:
from sqlalchemy.orm import (
    DeclarativeBase,  # базовый класс для ORM-моделей SQLAlchemy 2.0
    Mapped,  # типизированное ORM-поле SQLAlchemy 2.0.
    mapped_column,  # создаёт колонку таблицы в ORM-модели
    sessionmaker,  # создаёт фабрику сессий для работы с БД.
)

# ORM — Object Relational Mapping.

load_dotenv()


PORT = int(os.getenv("PORT", "8000"))
DATABASE_URL = os.getenv("DATABASE_URL", "")
THEMEALDB_URL = (
    f"https://www.themealdb.com/api/json/v1/{os.getenv('THEMEALDB_API_KEY', '1')}"
)
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"


def sqlalchemy_url():
    if not DATABASE_URL:
        raise RuntimeError("Add DATABASE_URL to .env.")
    if DATABASE_URL.startswith("postgresql://"):
        return DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    return DATABASE_URL


class Base(
    DeclarativeBase
):  # Базовый класс для ORM-моделей. Все модели наследуются от Base, чтобы SQLAlchemy видел их как таблицы и мог создать их через Base.metadata.create_all(engine).
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        Integer, Identity(always=True), primary_key=True
    )  # атрибут класса с аннотацией типа.
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class UserSession(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    # ORM-модель таблицы sessions: хранит токены авторизации пользователей.token — первичный ключ сессии, user_id — внешний ключ на users.id,created_at — время создания сессии в Unix timestamp.


# ONE TO MANY связь между такблицами - юзер один а сессий может быть много на разных устройств

engine = create_engine(
    sqlalchemy_url()
)  # create_engine() создаёт SQLAlchemy engine — объект, который управляет подключением и соединениями с PostgreSQL и через который ORM взаимодействует с базой данных.
SessionLocal = sessionmaker(
    bind=engine, expire_on_commit=False
)  # создаёт фабрику SessionLocal, через которую приложение создаёт ORM-сессии для выполнения запросов к PostgreSQL; expire_on_commit=False позволяет читать поля объектов после commit без повторной загрузки из БД.


def init_db():
    # Создает в БД все таблицы (users, sessions), привязанные к метаданным Base, если они отсутствуют
    Base.metadata.create_all(engine)


def password_hash(password):
    # Генерируем случайную соль
    # Соль нужна, чтобы одинаковые пароли (например, "123456")
    # у разных пользователей имели абсолютно разные хеши в БД.
    salt = secrets.token_hex(16)
    # Склеиваем соль с паролем и берем обычный SHA-256 хеш
    digest = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${digest}"


def password_ok(password, saved_hash):
    # Разделяем сохраненную строку на соль и старый хеш
    salt, old_digest = saved_hash.split("$", 1)
    # Считаем новый хеш с той же солью
    new_digest = hashlib.sha256((salt + password).encode()).hexdigest()
    # Безопасно сравниваем строки (защита от атак по времени)
    return secrets.compare_digest(new_digest, old_digest)


def user_by_email(email):
    # Открываем безопасную сессию для работы с БД
    with SessionLocal() as session:
        # Выполняем SELECT запрос с фильтрацией по email
        user = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()  # Возвращает объект User или None

    if not user:
        return None
    # Конвертируем данные в словарь, пока сессия закрыта
    return {"id": user.id, "email": user.email, "password_hash": user.password_hash}


def create_user(email, password):
    try:
        # Открывает сессию и СРАЗУ начинает транзакцию (авто-commit на выходе)
        with SessionLocal.begin() as session:
            user = User(
                email=email,
                password_hash=password_hash(password),
                created_at=int(time.time()),
            )
            session.add(user)
            # Синхронизируем с БД, чтобы PostgreSQL присвоил пользователю id
            session.flush()
            return user.id
    except IntegrityError:
        # Срабатывает, если email уже занят (нарушение UNIQUE в БД)
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
        # Запрос SELECT к users со связыванием (JOIN) таблицы sessions по id пользователя
        user = session.execute(
            select(User)
            .join(UserSession, User.id == UserSession.user_id)
            .where(UserSession.token == token)
        ).scalar_one_or_none()

    # Тернарный оператор: возвращает словарь, если user найден, иначе None
    return {"id": user.id, "email": user.email} if user else None


def normalize_meal(meal):
    ingredients = []
    # Цикл по 20 возможным парам ингредиент-мера в API TheMealDB
    for number in range(1, 21):
        # Безопасное извлечение строк с удалением пробелов по краям
        name = (meal.get(f"strIngredient{number}") or "").strip()
        measure = (meal.get(f"strMeasure{number}") or "").strip()
        # Исключение пустых ингредиентов из финального списка
        if name:
            ingredients.append({"name": name, "measure": measure})

    # Приведение структуры ответа к единому внутреннему формату приложения
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
    # Отправка GET-запроса с поисковым параметром и ограничением по времени (timeout)
    response = requests.get(
        f"{THEMEALDB_URL}/search.php", params={"s": query}, timeout=20
    )
    # Генерация исключения, если сервер вернул ошибку (код 4xx или 5xx)
    response.raise_for_status()
    # Извлечение списка блюд; если ключ 'meals' равен None или отсутствует, возвращается пустой список
    meals = response.json().get("meals") or []
    # Возврат нового списка с нормализованной структурой данных
    return [normalize_meal(meal) for meal in meals]


def recipe_text(recipe):
    # Формирование маркированного списка ингредиентов, разделенных переносом строки
    ingredients = "\n".join(
        f"- {item.get('measure', '')} {item.get('name', '')}".strip()
        for item in recipe.get("ingredients", [])
    )
    # Сборка финального текстового контекста (промта) из полей словаря
    return (
        f"Title: {recipe.get('title', '')}\n"
        f"Category: {recipe.get('category', '')}\n"
        f"Cuisine: {recipe.get('area', '')}\n"
        f"Ingredients:\n{ingredients}\n\n"
        f"Instructions:\n{recipe.get('instructions', '')}"
    )  # Примечание: в исходном коде не хватало закрывающей скобки )


def ask_mistral(recipe, question):
    # Безопасное извлечение API-ключа из переменных окружения (.env)
    api_key = os.getenv("MISTRAL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Add MISTRAL_API_KEY to .env.")

    # Отправка POST-запроса с авторизационным заголовком и JSON-телом
    response = requests.post(
        MISTRAL_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": os.getenv("MISTRAL_MODEL", "mistral-small-latest"),
            "temperature": 0.2,  # Низкая температура для минимизации галлюцинаций модели
            "messages": [
                {
                    "role": "system",
                    "content": "You answer questions about the provided recipe briefly and clearly.",
                },
                {
                    "role": "user",
                    # Конкатенация отформатированного рецепта и вопроса пользователя
                    "content": f"{recipe_text(recipe)}\n\nQuestion: {question}",
                },
            ],
        },
        timeout=60,  # Лимит ожидания ответа генерации — 60 секунд
    )
    # Проверка ответа на коды ошибок 4xx/5xx
    response.raise_for_status()
    # Извлечение текста ответа из структуры JSON-ответа Mistral API
    return response.json()["choices"][0]["message"]["content"].strip()
