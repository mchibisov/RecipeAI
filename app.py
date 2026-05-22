from flask import (  # Фласк-веб-фреймворк. Здесь я импортирую основные инструменты Flask. Flask создаёт приложение, render_template отдаёт HTML-страницы, request читает данные пользователя, redirect перенаправляет между страницами, а jsonify возвращает JSON-ответы для фронтенда. То есть этот импорт нужен для всей HTTP-логики проекта.
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
)

from core import (  # Эта часть импортирует из твоего файла core.py функции и константы, которые нужны в app.py.
    PORT,
    ask_mistral,
    create_session,
    create_user,
    current_user,
    delete_session,
    init_db,
    password_ok,
    search_meals,
    user_by_email,
)

app = Flask(
    __name__
)  # создает обьект/экземпляр класса фласк. __name__ — это служебная переменная Python, которая хранит имя текущего модуля. Если файл запущен напрямую, она равна "__main__", а если импортирован — имени файла. Во Flask я передаю __name__, чтобы Flask понимал, где находится приложение и где искать шаблоны и static-файлы


def json_body():
    return (
        request.get_json(silent=True) or {}
    )  # Получить JSON из запроса или вернуть пустой dict


def require_user():  # Проверяет авторизацию: получает session_token из cookie, находит по нему текущего пользователя и возвращает либо user, либо готовый HTTP-ответ с ошибкой 401 для route.
    user = current_user(request.cookies.get("session_token"))
    if not user:
        return None, (jsonify({"error": "Please sign in first."}), 401)  # Unauthorized
    return user, None


def auth_response(user_id, email):
    # Создание HTTP-ответа с данными пользователя в формате JSON
    response = jsonify({"user": {"id": user_id, "email": email}})
    # Установка защищенной cookie авторизации в браузер пользователя
    response.set_cookie(
        "session_token",
        create_session(user_id),  # Генерация токена и запись сессии в БД
        httponly=True,  # Защита от кражи токена через XSS (JS не имеет доступа)
        samesite="Lax",  # Защита от межсайтовой подделки запросов (CSRF)
    )
    return response


@app.get("/")
def index():
    # Проверка: если токен из cookie валиден и сессия существует в БД
    if current_user(request.cookies.get("session_token")):
        return redirect("/recipes")  # Перенаправление авторизованного пользователя
    return redirect("/auth")  # Отправка гостя на страницу входа/регистрации


@app.get("/auth")
def auth_page():
    if current_user(request.cookies.get("session_token")):
        return redirect("/recipes")
    return render_template("auth.html")


@app.get("/recipes")
def recipes_page():
    if not current_user(request.cookies.get("session_token")):
        return redirect("/auth")
    return render_template("recipes.html")


@app.get("/api/auth/me")
def auth_me():
    return jsonify({"user": current_user(request.cookies.get("session_token"))})


@app.post("/api/auth/register")
def auth_register():
    data = json_body()  # Парсинг входящего JSON-пакета
    email = (
        (data.get("email") or "").strip().lower()
    )  # Очистка и приведение email к нижнему регистру
    password = data.get("password") or ""

    # Валидация корректности формата email
    if "@" not in email:
        return jsonify({"error": "Enter a valid email."}), 400  # 400 Bad Request

    # Проверка минимальной длины пароля
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400

    # Попытка записи пользователя в БД (хэширование происходит внутри create_user)
    user_id = create_user(email, password)
    if not user_id:
        return jsonify(
            {"error": "Email is already registered."}
        ), 409  # 409 Conflict (дубликат)

    # Формирование успешного ответа и запись сессии в cookie
    return auth_response(user_id, email)


@app.post("/api/auth/login")
def auth_login():
    data = json_body()  # Извлечение входящих данных в формате JSON
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    # Поиск пользователя в БД по уникальному email
    user = user_by_email(email)

    # Проверка существования пользователя и валидация хэша пароля
    if not user or not password_ok(password, user["password_hash"]):
        return jsonify({"error": "Wrong email or password."}), 401  # 401 Unauthorized

    # Создание сессии, установка cookie и возврат успешного ответа
    return auth_response(user["id"], user["email"])


@app.post("/api/auth/logout")
def auth_logout():
    # Удаление сессии из базы данных (серверная деавторизация)
    delete_session(request.cookies.get("session_token"))

    # Формирование базового JSON-ответа
    response = jsonify({"ok": True})

    # Инструкция браузеру удалить cookie 'session_token' (клиентская деавторизация)
    response.delete_cookie("session_token")
    return response


@app.get("/api/recipes/search")
def recipes_search():
    # Проверка авторизации; префикс '_' означает, что сам объект пользователя нам здесь не нужен
    _, error = require_user()
    if error:
        return error  # Возврат ошибки 401, если токен невалиден

    # Получение значения параметра 'q' из URL-строки (например, /search?q=pizza)
    query = request.args.get("q", "").strip()

    # Тернарный оператор: поиск вызывается только при наличии непустой строки запроса
    return jsonify({"items": search_meals(query) if query else []})


@app.post("/api/ai")
def ai_prompt():
    # Проверка сессии текущего пользователя (блокировка неавторизованных запросов)
    _, error = require_user()
    if error:
        return error  # Возврат 401 Unauthorized

    data = json_body()  # Десериализация JSON-тела запроса
    recipe = data.get("recipe")  # Получение словаря с данными рецепта
    question = (
        data.get("question") or ""
    ).strip()  # Очистка текста вопроса от пробелов

    # Валидация входных данных на стороне бэкенда
    if not recipe:
        return jsonify({"error": "Choose a recipe."}), 400  # 400 Bad Request
    if not question:
        return jsonify({"error": "Enter a question."}), 400  # 400 Bad Request

    # Запрос к нейросети через API и возврат её текстового ответа клиенту
    return jsonify({"answer": ask_mistral(recipe, question)})


def run():
    # Инициализация БД: создание таблиц через SQLAlchemy перед запуском сервера
    init_db()

    # Запуск локального WSGI-сервера Flask на хосте 127.0.0.1 (localhost)
    # debug=False отключает вывод отладочной информации и автоперезапуск кода
    app.run(host="127.0.0.1", port=PORT, debug=False)


if __name__ == "__main__":
    run()
