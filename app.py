"""
Короткий серверный файл на Flask:
- принимает HTTP-запросы,
- отдаёт HTML через шаблоны,
- вызывает функции из core.py.
"""

# Flask - основной веб-фреймворк проекта.
# Ниже импортируем только те части, которые реально используем:
# Flask - создаёт объект приложения,
# jsonify - превращает Python-словари в JSON-ответы,
# redirect - делает HTTP-перенаправление между страницами,
# render_template - отдаёт HTML-файл из папки templates,
# request - даёт доступ к текущему HTTP-запросу: body, query, cookies и т.д.
from flask import Flask, jsonify, redirect, render_template, request

# HTTPException - специальный тип ошибок Flask/Werkzeug.
# Он нужен, чтобы в общем обработчике ошибок отличать "нормальные" HTTP-ошибки
# от неожиданных падений Python-кода.
from werkzeug.exceptions import HTTPException

# Из core.py импортируем всю прикладную логику, чтобы app.py оставался
# именно "слоем маршрутов", а не складом SQL, запросов к API и auth-логики.
from core import (
    PORT,  # Порт, на котором запускаем локальный Flask-сервер.
    ask_mistral,  # Один общий вызов Mistral по рецепту и prompt.
    create_session,  # Создание серверной сессии и token для cookie.
    create_user,  # Регистрация нового пользователя в PostgreSQL.
    current_user,  # Поиск текущего пользователя по token из cookie.
    delete_session,  # Удаление сессии при выходе из аккаунта.
    init_db,  # Создание таблиц при первом запуске приложения.
    meal_by_id,  # Загрузка полного рецепта из TheMealDB по meal_id.
    password_ok,  # Проверка, совпадает ли введённый пароль с hash в БД.
    save_recipe,  # Сохранение рецепта в локальную базу пользователя.
    search_meals,  # Поиск рецептов по строке запроса во внешнем API.
    user_by_email,  # Поиск пользователя по email для входа.
    user_recipe,  # Получение одного сохранённого рецепта пользователя.
    user_recipes,  # Получение всех сохранённых рецептов пользователя.
)

# Мы используем Flask как самый маленький и понятный веб-фреймворк.
# Он берёт на себя руты, шаблоны, cookies и JSON-ответы, поэтому код
# сервера получается заметно короче, чем на "ручном" http.server.
app = Flask(__name__)


# Этот helper нужен, чтобы в каждом POST-маршруте не писать одну и ту же
# защиту от пустого или сломанного JSON. Если тело запроса отсутствует
# или не похоже на JSON, мы просто работаем с пустым словарём.
def json_body():
    """Безопасно читаем JSON из запроса.

    Что принимает:
    - ничего явно; использует глобальный объект request от Flask.

    Что делает:
    - пытается прочитать JSON из тела текущего HTTP-запроса.
    - если JSON отсутствует или сломан, вместо падения возвращает пустой словарь.

    Что возвращает:
    - dict: данные запроса в виде словаря.
    """
    return request.get_json(silent=True) or {}


# Этот helper объединяет типичную логику для приватных API-методов:
# 1. достать session_token из cookie браузера,
# 2. по токену найти пользователя в БД,
# 3. если пользователя нет - сразу вернуть готовую 401 ошибку.
# Благодаря этому сами маршруты ниже остаются очень короткими.
def api_user():
    """Достаём текущего пользователя по cookie.

    Что принимает:
    - ничего явно; берёт session_token из request.cookies.

    Что делает:
    - ищет пользователя по токену сессии.
    - если пользователь не найден, сразу подготавливает готовую 401 ошибку.

    Что возвращает:
    - tuple[user | None, response | None]
    - первый элемент: словарь пользователя или None,
    - второй элемент: готовый Flask-response с ошибкой или None.
    """
    user = current_user(request.cookies.get("session_token"))
    if not user:
        return None, (jsonify({"error": "Please sign in first."}), 401)
    return user, None


# После входа или регистрации нам нужно сделать две вещи:
# 1. вернуть фронтенду краткую информацию о пользователе,
# 2. выдать браузеру cookie с токеном сессии.
# Эта функция делает обе задачи сразу, чтобы не дублировать код
# в двух auth-маршрутах.
def auth_response(user_id, email):
    """Формирует общий ответ для входа и регистрации.

    Что принимает:
    - user_id (int): id пользователя из базы.
    - email (str): email пользователя, который вернём фронтенду.

    Что делает:
    - создаёт JSON-ответ с данными пользователя,
    - создаёт новую серверную сессию,
    - ставит session_token в cookie браузера.

    Что возвращает:
    - Flask Response: готовый HTTP-ответ.
    """
    response = jsonify({"user": {"id": user_id, "email": email}})
    response.set_cookie(
        "session_token", create_session(user_id), httponly=True, samesite="Lax"
    )
    return response


@app.errorhandler(Exception)
def handle_error(error):
    """Единая обработка ошибок приложения.

    Что принимает:
    - error: любое исключение Python или Flask.

    Что делает:
    - если ошибка уже является штатной HTTP-ошибкой Flask, отдаёт её как есть,
    - если это API-маршрут, возвращает JSON с текстом ошибки,
    - если это обычная страница, возвращает простой текст.

    Что возвращает:
    - Flask Response или HTTPException.
    """
    if isinstance(error, HTTPException):
        return error
    if request.path.startswith("/api/"):
        return jsonify({"error": str(error)}), 500
    return str(error), 500


# Главная страница не содержит собственного интерфейса.
# Она просто смотрит: есть ли активная сессия. Если да - отправляем
# пользователя на рабочую страницу с рецептами, если нет - на auth.
@app.get("/")
def index():
    """Главная точка входа в приложение.

    Что принимает:
    - ничего явно; смотрит cookie у текущего запроса.

    Что делает:
    - проверяет, есть ли активная сессия,
    - отправляет пользователя либо на /recipes, либо на /auth.

    Что возвращает:
    - redirect response.
    """
    return redirect(
        "/recipes" if current_user(request.cookies.get("session_token")) else "/auth"
    )


# Одна HTML-страница обслуживает и вход, и регистрацию.
# Режим переключается на фронтенде JavaScript-кнопками.
@app.get("/auth")
def auth_page():
    """Отдаёт одну общую HTML-страницу для входа и регистрации.

    Что принимает:
    - ничего явно.

    Что делает:
    - если пользователь уже вошёл, не показывает auth-форму повторно,
    - иначе рендерит auth.html.

    Что возвращает:
    - redirect response или HTML response.
    """
    if current_user(request.cookies.get("session_token")):
        return redirect("/recipes")
    return render_template("auth.html")


# Страница рецептов доступна только после входа.
# Если пользователь пришёл без валидной cookie, отправляем его обратно
# на страницу авторизации.
@app.get("/recipes")
def recipes_page():
    """Отдаёт главную страницу с рецептами.

    Что принимает:
    - ничего явно.

    Что делает:
    - проверяет авторизацию,
    - если сессии нет, редиректит на auth,
    - если есть, рендерит recipes.html.

    Что возвращает:
    - redirect response или HTML response.
    """
    if not current_user(request.cookies.get("session_token")):
        return redirect("/auth")
    return render_template("recipes.html")


# Этот маршрут нужен фронтенду при загрузке страницы.
# Он отвечает на вопрос "кто сейчас вошёл в систему?".
# Если сессии нет, user будет равен null.
@app.get("/api/auth/me")
def auth_me():
    """Возвращает текущего пользователя для фронтенда.

    Что принимает:
    - ничего явно.

    Что делает:
    - смотрит cookie и по ней ищет активного пользователя.

    Что возвращает:
    - JSON вида {"user": ...} или {"user": null}.
    """
    return jsonify({"user": current_user(request.cookies.get("session_token"))})


# Поиск идёт не по нашей БД, а по внешнему API TheMealDB.
# Мы берём строку q из адресной строки и отдаём найденные рецепты в JSON.
@app.get("/api/recipes/search")
def recipes_search():
    """Ищет рецепты во внешнем API TheMealDB.

    Что принимает:
    - query-параметр q из URL, например /api/recipes/search?q=chicken

    Что делает:
    - проверяет, что пользователь вошёл,
    - отправляет строку поиска во внешний API,
    - возвращает найденные рецепты в JSON.

    Что возвращает:
    - JSON вида {"items": [...]}.
    """
    user, error = api_user()
    if error:
        return error
    text = request.args.get("q", "").strip()
    return jsonify({"items": search_meals(text) if text else []})


# Этот маршрут возвращает только сохранённые рецепты текущего пользователя
# из нашей PostgreSQL базы. То есть это уже не внешний API, а локальные данные.
@app.get("/api/recipes")
def recipes_list():
    """Возвращает сохранённые рецепты текущего пользователя.

    Что принимает:
    - ничего явно.

    Что делает:
    - проверяет авторизацию,
    - читает рецепты пользователя из PostgreSQL.

    Что возвращает:
    - JSON вида {"items": [...]}.
    """
    user, error = api_user()
    if error:
        return error
    return jsonify({"items": user_recipes(user["id"])})


# Регистрация максимально простая:
# 1. читаем email и password из JSON,
# 2. делаем базовую валидацию,
# 3. создаём пользователя,
# 4. сразу создаём сессию, чтобы не заставлять пользователя логиниться ещё раз.
@app.post("/api/auth/register")
def auth_register():
    """Регистрирует нового пользователя.

    Что принимает:
    - JSON body с полями:
      email (str)
      password (str)

    Что делает:
    - валидирует email и пароль,
    - создаёт пользователя,
    - сразу создаёт сессию после успешной регистрации.

    Что возвращает:
    - JSON с пользователем и cookie session_token
      или JSON-ошибку с кодом 400/409.
    """
    data = json_body()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if "@" not in email:
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters long."}), 400

    user_id = create_user(email, password)
    if not user_id:
        return jsonify({"error": "A user with this email already exists."}), 409

    return auth_response(user_id, email)


# Вход похож на регистрацию, но вместо создания нового пользователя
# мы проверяем, существует ли email и совпадает ли пароль с тем hash,
# который уже хранится в базе.
@app.post("/api/auth/login")
def auth_login():
    """Выполняет вход пользователя.

    Что принимает:
    - JSON body с email и password.

    Что делает:
    - ищет пользователя по email,
    - проверяет пароль по hash,
    - создаёт новую серверную сессию.

    Что возвращает:
    - JSON с пользователем и cookie session_token
      или JSON-ошибку 401.
    """
    data = json_body()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    row = user_by_email(email)

    if not row or not password_ok(password, row["password_hash"]):
        return jsonify({"error": "Wrong email or password."}), 401

    return auth_response(row["id"], row["email"])


# Выход - это удаление серверной сессии плюс удаление cookie у браузера.
@app.post("/api/auth/logout")
def auth_logout():
    """Выполняет выход пользователя.

    Что принимает:
    - ничего явно; использует session_token из cookie.

    Что делает:
    - удаляет сессию на сервере,
    - удаляет cookie у браузера.

    Что возвращает:
    - JSON {"ok": true}.
    """
    delete_session(request.cookies.get("session_token"))
    response = jsonify({"ok": True})
    response.delete_cookie("session_token")
    return response


# Здесь пользователь берёт рецепт из внешнего каталога TheMealDB
# и сохраняет его в свою локальную базу. После этого рецепт можно
# показывать в списке "saved recipes" и отправлять в Mistral.
@app.post("/api/recipes/import")
def recipes_import():
    """Сохраняет рецепт из TheMealDB в локальную базу.

    Что принимает:
    - JSON body с полем meal_id.

    Что делает:
    - проверяет авторизацию,
    - запрашивает полный рецепт по meal_id во внешнем API,
    - сохраняет его в PostgreSQL для текущего пользователя.

    Что возвращает:
    - JSON {"item": ...} и статус 201
      или JSON-ошибку 400/404.
    """
    user, error = api_user()
    if error:
        return error

    meal_id = json_body().get("meal_id")
    if not meal_id:
        return jsonify({"error": "Send meal_id in request body."}), 400

    recipe = meal_by_id(meal_id)
    if not recipe:
        return jsonify({"error": "Recipe not found."}), 404

    return jsonify({"item": save_recipe(user["id"], recipe)}), 201


# Это единая точка входа для AI.
# Фронтенд всегда присылает два поля:
# - recipe_id: какой сохранённый рецепт взять из базы,
# - prompt: что именно спросить у Mistral по этому рецепту.
# Один маршрут вместо нескольких делает код короче и проще.
@app.post("/api/ai")
def ai_prompt():
    """Один общий AI-маршрут.

    Что принимает:
    - JSON body с полями:
      recipe_id (int)
      prompt (str)

    Что делает:
    - проверяет авторизацию,
    - достаёт рецепт пользователя из базы,
    - отправляет рецепт и prompt в Mistral.

    Что возвращает:
    - JSON {"answer": "..."} или JSON-ошибку.
    """
    user, error = api_user()
    if error:
        return error

    data = json_body()
    recipe = user_recipe(user["id"], int(data.get("recipe_id") or 0))
    prompt = (data.get("prompt") or "").strip()

    if not recipe:
        return jsonify({"error": "Recipe was not found in your saved list."}), 404
    if not prompt:
        return jsonify({"error": "Enter a prompt for Mistral."}), 400

    return jsonify({"answer": ask_mistral(recipe, prompt)})


# Перед запуском сервера мы гарантируем, что нужные таблицы уже созданы.
# Это избавляет пользователя от отдельной команды "инициализации базы".
def run():
    """Запускает приложение.

    Что принимает:
    - ничего.

    Что делает:
    - создаёт таблицы, если их ещё нет,
    - запускает локальный Flask-сервер.

    Что возвращает:
    - ничего; функция блокирует поток, пока сервер работает.
    """
    init_db()
    app.run(host="127.0.0.1", port=PORT, debug=False)


if __name__ == "__main__":
    run()
