"""
Здесь лежит вся прикладная логика:
- чтение .env,
- PostgreSQL,
- auth и сессии,
- TheMealDB,
- Mistral.
"""

# hashlib - стандартная библиотека Python для хеширования.
# Здесь используется для безопасного хранения паролей.
import hashlib

# json - стандартная библиотека для работы с JSON-строками и словарями.
# Нужна для:
# - чтения/записи ingredients_json,
# - сборки payload для внешних API,
# - разбора JSON-ответов.
import json

# os - доступ к переменным окружения.
# Через os.getenv(...) мы читаем PORT, DATABASE_URL, API keys и другие настройки.
import os

# secrets - генерация криптографически стойких случайных значений.
# Используем для соли паролей и токенов сессий.
import secrets

# time - работа со временем.
# В проекте нужно в основном для Unix timestamp created_at.
import time

# Path из pathlib - удобная работа с файловыми путями.
# Используется для поиска файла .env рядом с текущим Python-файлом.
from pathlib import Path

# requests - маленькая и удобная библиотека для HTTP-запросов.
# Через неё ходим и в TheMealDB, и в Mistral.
import requests

# SQLAlchemy - библиотека, которая работает как прослойка между Python-кодом
# и PostgreSQL. Её часто называют ORM: Object-Relational Mapper.
# Очень простыми словами:
# - Object: в Python мы работаем с объектами, например User или Recipe.
# - Relational: в PostgreSQL данные лежат в таблицах users, sessions, recipes.
# - Mapper: SQLAlchemy связывает Python-классы с таблицами базы данных.
#
# Поэтому вместо того, чтобы везде руками писать длинные SQL-строки
# "SELECT ...", "INSERT ...", "DELETE ...", мы описываем структуру таблиц
# Python-классами, а SQLAlchemy сама превращает наши действия в SQL-запросы.
#
# Важно: SQLAlchemy не заменяет PostgreSQL. PostgreSQL всё ещё остаётся базой.
# SQLAlchemy просто даёт более удобный Python-слой для работы с этой базой.
from sqlalchemy import (
    ForeignKey,
    Identity,
    Integer,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


# Мы вручную читаем .env, потому что для учебного проекта это проще,
# чем тащить ещё одну библиотеку только ради пары переменных окружения.
# Логика очень простая: открыть файл, пройтись по строкам и сложить пары
# KEY=VALUE в os.environ, чтобы потом весь остальной код работал через os.getenv().
def load_env():
    """Читает локальный файл .env и добавляет переменные в окружение.

    Что принимает:
    - ничего.

    Что делает:
    - ищет файл .env рядом с проектом,
    - читает его построчно,
    - пропускает пустые строки и комментарии,
    - складывает пары KEY=VALUE в os.environ.

    Что возвращает:
    - ничего.
    """
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


# Эти константы читаются один раз при старте приложения.
# Дальше весь проект использует уже готовые значения, а не перечитывает
# .env в каждом запросе. Так код получается и короче, и быстрее.
PORT = int(os.getenv("PORT", "8000"))
DATABASE_URL = os.getenv("DATABASE_URL", "")
THEMEALDB_BASE_URL = f"https://www.themealdb.com/api/json/v1/{os.getenv('THEMEALDB_API_KEY', '1')}"
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

# По умолчанию сертификаты проверяются нормально.
# Если Python на конкретной машине ругается на SSL, можно включить ALLOW_INSECURE_SSL=1.
VERIFY_SSL = os.getenv("ALLOW_INSECURE_SSL") != "1"


# SQLAlchemy сама не "говорит" с PostgreSQL напрямую на низком уровне.
# Ей нужен драйвер. В этом проекте драйвером остаётся psycopg.
#
# Поэтому в requirements.txt есть и SQLAlchemy, и psycopg[binary]:
# - SQLAlchemy отвечает за ORM, модели, сессии и удобные запросы.
# - psycopg отвечает за реальное сетевое подключение к PostgreSQL.
#
# SQLAlchemy с драйвером psycopg ожидает URL вида:
# postgresql+psycopg://user@localhost:5432/database
#
# Но в .env у нас обычная учебная запись:
# postgresql://user@localhost:5432/database
#
# Функция ниже аккуратно добавляет "+psycopg", чтобы пользователю не нужно
# было помнить специальный формат SQLAlchemy.
def sqlalchemy_url():
    """Готовит адрес подключения для SQLAlchemy.

    Что принимает:
    - ничего.

    Что делает:
    - проверяет, что DATABASE_URL задан,
    - добавляет имя драйвера psycopg в адрес PostgreSQL.

    Что возвращает:
    - str: URL подключения для SQLAlchemy.
    """
    if not DATABASE_URL:
        raise RuntimeError("Add DATABASE_URL to your .env file.")
    if DATABASE_URL.startswith("postgresql://"):
        return DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    return DATABASE_URL


class Base(DeclarativeBase):
    """Базовый класс для всех SQLAlchemy-моделей проекта."""


# Base - это общий родитель для всех моделей.
# Модель - это Python-класс, который описывает одну таблицу в базе данных.
# Когда класс наследуется от Base, SQLAlchemy начинает "видеть" этот класс
# как таблицу и добавляет его описание в Base.metadata.
#
# Base.metadata - это общий список всех таблиц, которые описаны в коде.
# Позже строка Base.metadata.create_all(engine) создаёт эти таблицы в PostgreSQL,
# если их ещё нет.


class User(Base):
    """Модель таблицы users."""

    # __tablename__ говорит SQLAlchemy, с какой таблицей PostgreSQL связан
    # этот Python-класс. Здесь класс User связан с таблицей users.
    __tablename__ = "users"

    # Mapped[int] - подсказка типов: в Python это поле будет числом.
    # mapped_column(...) - описание колонки в PostgreSQL.
    #
    # Identity(always=True) соответствует PostgreSQL-конструкции:
    # GENERATED ALWAYS AS IDENTITY
    # То есть id генерируется самой базой автоматически.
    #
    # primary_key=True означает, что id является главным ключом таблицы.
    id: Mapped[int] = mapped_column(
        Integer,
        Identity(always=True),
        primary_key=True,
    )
    # nullable=False означает NOT NULL: поле обязательно должно быть заполнено.
    # unique=True означает UNIQUE: двух пользователей с одинаковым email быть не может.
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class UserSession(Base):
    """Модель таблицы sessions."""

    __tablename__ = "sessions"

    # token - главный ключ таблицы sessions.
    # В cookie браузера хранится именно token, а не id пользователя.
    token: Mapped[str] = mapped_column(Text, primary_key=True)
    # ForeignKey("users.id") означает внешний ключ:
    # sessions.user_id ссылается на users.id.
    #
    # ondelete="CASCADE" означает: если пользователь удалён из users,
    # его сессии автоматически удалятся из sessions.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class Recipe(Base):
    """Модель таблицы recipes."""

    __tablename__ = "recipes"
    # UniqueConstraint("user_id", "meal_id") означает составное ограничение:
    # один и тот же пользователь не может сохранить один и тот же рецепт дважды.
    # Но другой пользователь может сохранить такой же meal_id у себя.
    __table_args__ = (UniqueConstraint("user_id", "meal_id"),)

    id: Mapped[int] = mapped_column(
        Integer,
        Identity(always=True),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    meal_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    area: Mapped[str | None] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[str | None] = mapped_column(Text)
    youtube_url: Mapped[str | None] = mapped_column(Text)
    ingredients_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


# engine - центральный объект SQLAlchemy для подключения к базе.
# Это не одно открытое соединение, а "фабрика подключений" и пул соединений.
# Когда коду нужна база, SQLAlchemy берёт подключение через engine.
engine = create_engine(sqlalchemy_url(), future=True)

# SessionLocal - это фабрика сессий.
# Сессия SQLAlchemy - это рабочая область для операций с базой:
# - через неё выполняют select(...), delete(...), session.add(...);
# - она следит за изменёнными объектами;
# - она делает commit, rollback и закрывает ресурсы.
#
# bind=engine связывает сессию с нашей PostgreSQL-базой.
#
# expire_on_commit=False означает: после commit значения полей объекта
# не будут сразу "протухать". Для маленького Flask-проекта так проще:
# мы можем вернуть user.id или поля рецепта сразу после записи.
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# При старте проекта SQLAlchemy создаёт три минимально необходимые таблицы:
# users, sessions и recipes. Таблицы описаны выше как Python-классы, поэтому
# отдельные CREATE TABLE строки больше не нужны.
def init_db():
    """Создаёт таблицы проекта, если они ещё не существуют.

    Что принимает:
    - ничего.

    Что делает:
    - создаёт users,
    - создаёт sessions,
    - создаёт recipes.

    Что возвращает:
    - ничего.
    """
    # create_all смотрит на все модели, которые наследуются от Base:
    # User, UserSession, Recipe.
    #
    # Если таблиц users, sessions, recipes ещё нет, SQLAlchemy создаст их.
    # Если таблицы уже есть, create_all не удаляет их и не стирает данные.
    # Это похоже на CREATE TABLE IF NOT EXISTS, только через ORM-модели.
    Base.metadata.create_all(engine)


# Ниже идёт блок функций для паролей.
# Пароль нельзя хранить как обычный текст, поэтому мы делаем hash.
# Отдельная функция hash_value() нужна, чтобы не дублировать одну и ту же формулу
# в password_hash() и password_ok().
def hash_value(password, salt):
    """Считает hash для пароля и соли.

    Что принимает:
    - password (str): исходный пароль пользователя.
    - salt (str): случайная соль.

    Что делает:
    - считает PBKDF2-HMAC-SHA256 hash.

    Что возвращает:
    - str: hash в hex-формате.
    """
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120_000,
    ).hex()


def password_hash(password):
    """Готовит строку для сохранения пароля в базе.

    Что принимает:
    - password (str): пароль в открытом виде.

    Что делает:
    - генерирует соль,
    - считает hash,
    - склеивает их в строку salt$hash.

    Что возвращает:
    - str: строка salt$hash.
    """
    salt = secrets.token_hex(16)
    return f"{salt}${hash_value(password, salt)}"


def password_ok(password, saved_hash):
    """Проверяет, совпадает ли введённый пароль с тем, что в базе.

    Что принимает:
    - password (str): пароль, который ввёл пользователь.
    - saved_hash (str): строка salt$hash из БД.

    Что делает:
    - выделяет соль,
    - считает новый hash,
    - сравнивает с сохранённым значением.

    Что возвращает:
    - bool: True если пароль верный, иначе False.
    """
    salt, digest = saved_hash.split("$", 1)
    return secrets.compare_digest(hash_value(password, salt), digest)


# Этот небольшой запрос нужен в двух сценариях:
# - когда пользователь пытается войти,
# - когда пользователь пытается зарегистрироваться.
# Поэтому мы вынесли его в отдельную функцию, чтобы SQL не повторялся.
def user_by_email(email):
    """Ищет пользователя по email.

    Что принимает:
    - email (str): адрес пользователя.

    Что делает:
    - выполняет SELECT в таблице users.

    Что возвращает:
    - dict или None.
    """
    # with SessionLocal() as session:
    #
    # Здесь SessionLocal() создаёт новую SQLAlchemy-сессию для работы с базой.
    # Слово with включает context manager - специальный механизм Python,
    # который гарантирует выполнение "уборки" после блока.
    #
    # Что происходит по шагам:
    # 1. создаётся session;
    # 2. внутри блока выполняется запрос SELECT;
    # 3. когда блок заканчивается, session автоматически закрывается.
    #
    # Здесь мы только читаем данные, поэтому достаточно SessionLocal().
    # Commit не нужен, потому что SELECT ничего не меняет в базе.
    with SessionLocal() as session:
        # select(User) означает "выбрать строки из таблицы users".
        # where(User.email == email) добавляет условие WHERE email = ...
        #
        # session.execute(...) отправляет запрос в PostgreSQL через SQLAlchemy.
        # scalar_one_or_none() возвращает:
        # - один объект User, если пользователь найден;
        # - None, если пользователя нет;
        # - ошибку, если неожиданно найдено больше одной строки.
        user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if not user:
        return None
    return {"id": user.id, "email": user.email, "password_hash": user.password_hash}


def create_user(email, password):
    """Создаёт нового пользователя.

    Что принимает:
    - email (str)
    - password (str)

    Что делает:
    - проверяет, что такого email ещё нет,
    - считает hash пароля,
    - вставляет запись в таблицу users.

    Что возвращает:
    - int: id нового пользователя
    - или None, если email уже занят.
    """
    if user_by_email(email):
        return None

    try:
        # with SessionLocal.begin() as session:
        #
        # begin() нужен для операций, которые меняют базу: INSERT, UPDATE, DELETE.
        # Он открывает транзакцию.
        #
        # Транзакция - это группа действий с базой, которая должна завершиться
        # целиком. Если внутри блока всё прошло хорошо, SQLAlchemy сделает commit.
        # Если внутри блока случилась ошибка, SQLAlchemy сделает rollback.
        #
        # Поэтому этот with заменяет ручной код примерно такого вида:
        #
        # session = SessionLocal()
        # try:
        #     ...работа с базой...
        #     session.commit()
        # except:
        #     session.rollback()
        #     raise
        # finally:
        #     session.close()
        #
        # То есть with здесь нужен не для красоты, а чтобы не забыть commit,
        # rollback и close.
        with SessionLocal.begin() as session:
            user = User(
                email=email,
                password_hash=password_hash(password),
                created_at=int(time.time()),
            )
            # session.add(user) говорит SQLAlchemy:
            # "этот новый Python-объект нужно вставить в таблицу users".
            session.add(user)
            # session.flush() отправляет INSERT в базу до конца блока.
            # Это нужно, чтобы PostgreSQL успел сгенерировать user.id,
            # и мы могли вернуть id нового пользователя.
            # Commit всё равно произойдёт автоматически при выходе из with.
            session.flush()
            return user.id
    except IntegrityError:
        # IntegrityError может возникнуть, например, если два запроса одновременно
        # пытаются создать пользователя с одним email. В таблице стоит unique=True,
        # поэтому PostgreSQL не даст создать дубль.
        return None


# Сессия в этом проекте серверная.
# Это значит, что в cookie браузера хранится только случайный token,
# а реальные данные о сессии лежат у нас в таблице sessions.
# Такой подход понятен для защиты: есть таблица, есть token, есть связь с user_id.
def create_session(user_id):
    """Создаёт серверную сессию для пользователя.

    Что принимает:
    - user_id (int): id пользователя.

    Что делает:
    - генерирует случайный token,
    - записывает token и user_id в таблицу sessions.

    Что возвращает:
    - str: token сессии.
    """
    token = secrets.token_urlsafe(32)
    # Здесь мы записываем новую строку в sessions, поэтому используем begin().
    # Если session.add(...) выполнится успешно, при выходе из with будет commit.
    # Если PostgreSQL вернёт ошибку, будет rollback и соединение закроется.
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
    """Удаляет сессию по token.

    Что принимает:
    - token (str | None): токен из cookie.

    Что делает:
    - если токена нет, ничего не делает,
    - если токен есть, удаляет запись из sessions.

    Что возвращает:
    - ничего.
    """
    if not token:
        return
    # Удаление меняет базу, поэтому снова используем SessionLocal.begin().
    # session.execute(delete(...)) будет превращён SQLAlchemy в SQL DELETE.
    # Commit произойдёт автоматически при успешном выходе из блока with.
    with SessionLocal.begin() as session:
        session.execute(delete(UserSession).where(UserSession.token == token))


# Эта функция делает обратную операцию к create_session():
# по токену из cookie находит, какому пользователю принадлежит текущая сессия.
# Именно через неё сервер понимает, авторизован пользователь или нет.
def current_user(token):
    """Находит пользователя по token сессии.

    Что принимает:
    - token (str | None): session token из cookie.

    Что делает:
    - соединяет таблицы sessions и users,
    - ищет пользователя, которому принадлежит токен.

    Что возвращает:
    - dict с id и email пользователя
    - или None, если сессия не найдена.
    """
    if not token:
        return None

    # Здесь база только читается: мы ищем пользователя по токену сессии.
    # Поэтому используем обычный with SessionLocal(), без begin().
    #
    # Даже для чтения with полезен: он закрывает session после запроса.
    # Если сессии не закрывать, в долгой работе приложения могут копиться
    # открытые подключения к PostgreSQL.
    with SessionLocal() as session:
        # select(User) говорит, что в результате нам нужен объект User.
        #
        # join(UserSession, User.id == UserSession.user_id) соединяет таблицы:
        # users JOIN sessions ON users.id = sessions.user_id
        #
        # where(UserSession.token == token) оставляет только нужную сессию.
        user = session.execute(
            select(User)
            .join(UserSession, User.id == UserSession.user_id)
            .where(UserSession.token == token)
        ).scalar_one_or_none()
    return {"id": user.id, "email": user.email} if user else None


# Это единый helper для всех внешних HTTP-запросов.
# Он нужен, чтобы и TheMealDB, и Mistral вызывались одним и тем же способом.
# requests здесь заметно упрощает код по сравнению со стандартным urllib:
# меньше строк, понятнее параметры, удобнее обработка ошибок.
# Внутри есть и простой запасной путь для SSL: если локальный Python не умеет
# нормально проверять сертификаты, можно один раз повторить запрос без проверки.
def fetch_json(url, method="GET", payload=None, headers=None, params=None, timeout=30):
    """Делает HTTP-запрос и возвращает JSON.

    Что принимает:
    - url (str): адрес запроса.
    - method (str): GET или POST.
    - payload (dict | None): JSON-тело запроса.
    - headers (dict | None): HTTP-заголовки.
    - params (dict | None): query-параметры.
    - timeout (int): таймаут в секундах.

    Что делает:
    - собирает общий запрос для requests,
    - отправляет его,
    - при необходимости повторяет запрос с ослабленной SSL-проверкой.

    Что возвращает:
    - dict или list: распарсенный JSON-ответ.
    """
    options = {
        "method": method,
        "url": url,
        "json": payload,
        "params": params,
        "headers": headers or {"User-Agent": "RecipeApp/1.0"},
        "timeout": timeout,
    }

    def call(verify_ssl):
        response = requests.request(**options, verify=verify_ssl)
        response.raise_for_status()
        return response.json()

    try:
        return call(VERIFY_SSL)
    except requests.exceptions.SSLError:
        if VERIFY_SSL:
            return call(False)
        raise


# TheMealDB отдаёт рецепт в сыром формате, где ингредиенты лежат в полях
# strIngredient1, strIngredient2, ... strIngredient20.
# Для фронтенда и базы такой формат неудобен, поэтому мы один раз приводим
# ответ к своему компактному словарю с понятными именами полей.
def normalize_meal(meal):
    """Приводит сырой рецепт TheMealDB к удобному формату проекта.

    Что принимает:
    - meal (dict): один рецепт в формате TheMealDB.

    Что делает:
    - собирает список ингредиентов,
    - переименовывает поля в более понятные имена.

    Что возвращает:
    - dict: нормализованный рецепт.
    """
    ingredients = []
    for index in range(1, 21):
        name = (meal.get(f"strIngredient{index}") or "").strip()
        if name:
            ingredients.append(
                {
                    "name": name,
                    "measure": (meal.get(f"strMeasure{index}") or "").strip(),
                }
            )

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


# Поиск идёт по названию рецепта через TheMealDB search.php?s=...
# Если API вернул пустой список или null, мы просто отдаём пустой массив,
# чтобы фронтенду не пришлось отдельно обрабатывать None.
def search_meals(query):
    """Ищет рецепты по названию во внешнем API.

    Что принимает:
    - query (str): текст поиска.

    Что делает:
    - отправляет запрос в TheMealDB search.php,
    - нормализует каждый найденный рецепт.

    Что возвращает:
    - list[dict]: список рецептов.
    """
    meals = fetch_json(f"{THEMEALDB_BASE_URL}/search.php", params={"s": query}).get("meals") or []
    return [normalize_meal(meal) for meal in meals]


# Здесь получаем уже один конкретный рецепт по meal_id.
# Это используется перед сохранением рецепта в нашу локальную базу.
def meal_by_id(meal_id):
    """Загружает один полный рецепт по meal_id.

    Что принимает:
    - meal_id (str | int): id рецепта из TheMealDB.

    Что делает:
    - отправляет lookup-запрос во внешний API,
    - берёт первый рецепт из ответа.

    Что возвращает:
    - dict: нормализованный рецепт
    - или None, если рецепт не найден.
    """
    meals = fetch_json(f"{THEMEALDB_BASE_URL}/lookup.php", params={"i": meal_id}).get("meals") or []
    return normalize_meal(meals[0]) if meals else None


# В PostgreSQL ингредиенты мы храним как JSON-строку в поле ingredients_json,
# потому что так проще держать структуру в одной таблице без дополнительных связей.
# Перед отправкой во фронтенд превращаем её обратно в обычный список Python/JSON.
def recipe_row(recipe):
    """Превращает SQLAlchemy-объект Recipe в словарь для API.

    Что принимает:
    - recipe (Recipe): объект рецепта из таблицы recipes.

    Что делает:
    - берёт нужные поля из ORM-объекта,
    - превращает ingredients_json обратно в список ingredients.

    Что возвращает:
    - dict: готовый рецепт для фронтенда.
    """
    return {
        "id": recipe.id,
        "user_id": recipe.user_id,
        "meal_id": recipe.meal_id,
        "title": recipe.title,
        "category": recipe.category,
        "area": recipe.area,
        "instructions": recipe.instructions,
        "image_url": recipe.image_url,
        "youtube_url": recipe.youtube_url,
        "created_at": recipe.created_at,
        "ingredients": json.loads(recipe.ingredients_json),
    }


# Эта функция сохраняет рецепт в локальную базу пользователя.
# Если пользователь уже сохранял этот же рецепт раньше, мы не создаём дубликат,
# а обновляем существующую запись.
#
# В старой версии это было сделано ручным SQL через ON CONFLICT.
# В версии с SQLAlchemy логика такая же, но записана через ORM:
# 1. сначала ищем существующий Recipe по user_id и meal_id;
# 2. если нашли - меняем поля найденного объекта;
# 3. если не нашли - создаём новый объект Recipe и добавляем его в session;
# 4. SQLAlchemy сама понимает, где нужен INSERT, а где UPDATE.
# Так поведение остаётся стабильным, а данные в базе не засоряются дублями.
def save_recipe(user_id, recipe):
    """Сохраняет рецепт в локальную базу пользователя.

    Что принимает:
    - user_id (int): владелец рецепта.
    - recipe (dict): нормализованный рецепт из TheMealDB.

    Что делает:
    - сериализует ингредиенты в JSON,
    - вставляет рецепт в таблицу recipes,
    - при конфликте обновляет существующую запись.

    Что возвращает:
    - dict: сохранённый рецепт в формате проекта.
    """
    ingredients_json = json.dumps(recipe["ingredients"], ensure_ascii=False)
    # Здесь мы либо вставляем новый рецепт, либо обновляем существующий.
    # Обе операции меняют базу, поэтому используем SessionLocal.begin().
    # begin() автоматически делает commit при успехе и rollback при ошибке.
    with SessionLocal.begin() as session:
        # Сначала проверяем, есть ли уже такой рецепт у этого пользователя.
        # Это ORM-аналог SQL-запроса:
        #
        # SELECT * FROM recipes
        # WHERE user_id = ... AND meal_id = ...
        #
        # scalar_one_or_none() вернёт объект Recipe или None.
        saved_recipe = session.execute(
            select(Recipe).where(
                Recipe.user_id == user_id,
                Recipe.meal_id == recipe["meal_id"],
            )
        ).scalar_one_or_none()

        if not saved_recipe:
            # Если записи нет, создаём Python-объект Recipe.
            # Пока мы просто создали объект в памяти Python.
            # В базу он попадёт после session.add(...) и flush/commit.
            saved_recipe = Recipe(
                user_id=user_id,
                meal_id=recipe["meal_id"],
                created_at=int(time.time()),
            )
            # session.add(saved_recipe) говорит SQLAlchemy:
            # "этот объект нужно вставить в таблицу recipes".
            session.add(saved_recipe)

        # Дальше мы заполняем поля объекта.
        # Если объект новый, SQLAlchemy сделает INSERT.
        # Если объект уже был взят из базы, SQLAlchemy заметит изменения
        # и сделает UPDATE при commit.
        saved_recipe.title = recipe["title"]
        saved_recipe.category = recipe["category"]
        saved_recipe.area = recipe["area"]
        saved_recipe.instructions = recipe["instructions"]
        saved_recipe.image_url = recipe["image_url"]
        saved_recipe.youtube_url = recipe["youtube_url"]
        saved_recipe.ingredients_json = ingredients_json
        # flush отправляет изменения в PostgreSQL прямо сейчас, но ещё не
        # закрывает транзакцию. Это удобно, если нужен id или актуальные поля.
        # Финальный commit всё равно делает with SessionLocal.begin().
        session.flush()

        return recipe_row(saved_recipe)


# Фронтенду нужен список всех ранее сохранённых рецептов пользователя.
# Поэтому здесь просто обычный SELECT с сортировкой: новые записи выше.
def user_recipes(user_id):
    """Возвращает все сохранённые рецепты пользователя.

    Что принимает:
    - user_id (int): id пользователя.

    Что делает:
    - читает все записи recipes для этого пользователя.

    Что возвращает:
    - list[dict]: список рецептов.
    """
    # Здесь мы только читаем список рецептов, поэтому begin() не нужен.
    # Обычный with SessionLocal() создаёт session на время запроса и закрывает её
    # после выхода из блока.
    with SessionLocal() as session:
        # scalars().all() означает:
        # - взять из результата именно ORM-объекты Recipe,
        # - вернуть их обычным Python-списком.
        recipes = session.execute(
            select(Recipe).where(Recipe.user_id == user_id).order_by(Recipe.id.desc())
        ).scalars().all()
    return [recipe_row(recipe) for recipe in recipes]


# А эта функция нужна, когда фронтенд или AI работает уже с одним конкретным
# сохранённым рецептом. Мы всегда фильтруем и по recipe_id, и по user_id,
# чтобы пользователь не мог получить чужую запись.
def user_recipe(user_id, recipe_id):
    """Возвращает один конкретный рецепт пользователя.

    Что принимает:
    - user_id (int)
    - recipe_id (int)

    Что делает:
    - ищет одну запись по user_id и id.

    Что возвращает:
    - dict: рецепт
    - или None, если такой записи нет.
    """
    # Здесь тоже только чтение: ищем один рецепт текущего пользователя.
    # Фильтр идёт сразу по user_id и recipe_id, чтобы пользователь не мог
    # получить чужой рецепт, просто угадав id.
    with SessionLocal() as session:
        recipe = session.execute(
            select(Recipe).where(
                Recipe.user_id == user_id,
                Recipe.id == recipe_id,
            )
        ).scalar_one_or_none()
    return recipe_row(recipe) if recipe else None


# Большие языковые модели удобнее работают не с "разрозненными полями",
# а с одним цельным текстом. Поэтому мы собираем рецепт в понятный текстовый блок:
# название, кухня, ингредиенты и инструкция.
def recipe_text(recipe):
    """Собирает рецепт в один текстовый блок для Mistral.

    Что принимает:
    - recipe (dict): рецепт из нашей базы.

    Что делает:
    - собирает ингредиенты в многострочный список,
    - добавляет название, категорию, кухню и инструкцию.

    Что возвращает:
    - str: цельный текст рецепта.
    """
    ingredients = "\n".join(
        f"- {item['measure']} {item['name']}".strip()
        for item in recipe["ingredients"]
    )
    return (
        f"Title: {recipe['title']}\n"
        f"Category: {recipe['category'] or 'not specified'}\n"
        f"Cuisine: {recipe['area'] or 'not specified'}\n"
        f"Ingredients:\n{ingredients}\n\n"
        f"Instructions:\n{recipe['instructions']}"
    )


# Это единый вызов Mistral для всех AI-сценариев проекта.
# Снаружи неважно, была ли это быстрая кнопка "simplify" или свободный вопрос:
# мы всегда отправляем recipe_text + prompt в одну и ту же функцию.
# Так код приложения остаётся намного короче.
def ask_mistral(recipe, user_prompt):
    """Отправляет рецепт и prompt в Mistral.

    Что принимает:
    - recipe (dict): выбранный рецепт.
    - user_prompt (str): вопрос или задача для модели.

    Что делает:
    - проверяет MISTRAL_API_KEY,
    - собирает payload для chat completions,
    - отправляет POST-запрос в Mistral,
    - достаёт текст ответа из choices.

    Что возвращает:
    - str: готовый ответ модели.
    """
    api_key = os.getenv("MISTRAL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Add MISTRAL_API_KEY to your .env file.")

    payload = {
        "model": os.getenv("MISTRAL_MODEL", "mistral-small-latest"),
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": "You are a recipe helper. Reply in English, clearly and briefly. Use only the recipe provided in the prompt.",
            },
            {
                "role": "user",
                "content": f"{recipe_text(recipe)}\n\nQuestion or task:\n{user_prompt}",
            },
        ],
    }

    try:
        result = fetch_json(
            MISTRAL_API_URL,
            method="POST",
            payload=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60,
        )
    except requests.HTTPError as error:
        body = error.response.text if error.response is not None else str(error)
        raise RuntimeError(f"Mistral returned an error: {body}") from error
    except requests.RequestException as error:
        raise RuntimeError(f"Could not connect to Mistral: {error}") from error

    choices = result.get("choices") or []
    if not choices:
        raise RuntimeError("Mistral did not return an answer.")
    return choices[0]["message"]["content"].strip()
