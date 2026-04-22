"""
Короткий серверный файл:
- принимает HTTP-запросы,
- раздаёт HTML и CSS,
- вызывает функции из core.py.
"""

import json
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from core import (
    AI_TASKS,
    PORT,
    ask_mistral,
    create_session,
    create_user,
    current_user,
    delete_session,
    find_user_by_email,
    init_db,
    meal_by_id,
    password_ok,
    read_static,
    read_template,
    save_recipe,
    search_meals,
    user_recipe,
    user_recipes,
)

PAGE_TEMPLATES = {
    "/login": "login.html",
    "/register": "register.html",
}


def send_bytes(handler, body, content_type, status=HTTPStatus.OK, headers=None):
    """Базовый helper для ответа."""
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if headers:
        for key, value in headers.items():
            handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def send_json(handler, payload, status=HTTPStatus.OK, headers=None):
    """Один общий способ отправить JSON."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Cache-Control": "no-store", **(headers or {})}
    send_bytes(handler, body, "application/json; charset=utf-8", status, headers)


def send_html(handler, html_text):
    """Отдаём HTML-страницу."""
    send_bytes(handler, html_text.encode("utf-8"), "text/html; charset=utf-8")


def send_css(handler, css_text):
    """Отдаём CSS-файл."""
    send_bytes(handler, css_text.encode("utf-8"), "text/css; charset=utf-8")


def redirect(handler, location):
    """Редирект между страницами."""
    handler.send_response(HTTPStatus.SEE_OTHER)
    handler.send_header("Location", location)
    handler.end_headers()


def read_json(handler):
    """Читаем JSON из POST-запроса."""
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        return {}

    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("Тело запроса должно быть в формате JSON.") from error


def session_token(handler):
    """Берём session token из cookie."""
    cookie = SimpleCookie()
    cookie.load(handler.headers.get("Cookie", ""))
    value = cookie.get("session_token")
    return value.value if value else None


def require_user(handler):
    """Для API проверяем, что пользователь вошёл."""
    user = current_user(session_token(handler))
    if not user:
        send_json(
            handler, {"error": "Сначала войдите в аккаунт."}, HTTPStatus.UNAUTHORIZED
        )
        return None
    return user


def set_session_headers(token):
    """Cookie для новой сессии."""
    return {"Set-Cookie": f"session_token={token}; HttpOnly; Path=/; SameSite=Lax"}


def clear_session_headers():
    """Cookie для выхода."""
    return {"Set-Cookie": "session_token=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax"}


class Handler(BaseHTTPRequestHandler):
    """Один обработчик маршрутов для всего проекта."""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        user = current_user(session_token(self))

        try:
            if path == "/":
                redirect(self, "/recipes" if user else "/login")
                return

            if path in PAGE_TEMPLATES:
                if user:
                    redirect(self, "/recipes")
                else:
                    send_html(self, read_template(PAGE_TEMPLATES[path]))
                return

            if path == "/recipes":
                if not user:
                    redirect(self, "/login")
                else:
                    send_html(self, read_template("recipes.html"))
                return

            if path == "/static/styles.css":
                send_css(self, read_static("styles.css"))
                return

            if path == "/api/auth/me":
                send_json(self, {"user": user})
                return

            if path == "/api/recipes/search":
                user = require_user(self)
                if not user:
                    return
                text = (query.get("q", [""])[0]).strip()
                send_json(self, {"items": search_meals(text) if text else []})
                return

            if path == "/api/recipes":
                user = require_user(self)
                if not user:
                    return
                send_json(self, {"items": user_recipes(user["id"])})
                return

            send_json(self, {"error": "Маршрут не найден."}, HTTPStatus.NOT_FOUND)
        except Exception as error:  # noqa: BLE001
            send_json(self, {"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        path = urlparse(self.path).path

        try:
            if path == "/api/auth/register":
                data = read_json(self)
                email = (data.get("email") or "").strip().lower()
                password = data.get("password") or ""

                if "@" not in email:
                    send_json(
                        self,
                        {"error": "Введите корректный email."},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                if len(password) < 6:
                    send_json(
                        self,
                        {"error": "Пароль должен быть не короче 6 символов."},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return

                user_id = create_user(email, password)
                if not user_id:
                    send_json(
                        self,
                        {"error": "Пользователь с таким email уже существует."},
                        HTTPStatus.CONFLICT,
                    )
                    return

                token = create_session(user_id)
                send_json(
                    self,
                    {"user": {"id": user_id, "email": email}},
                    headers=set_session_headers(token),
                )
                return

            if path == "/api/auth/login":
                data = read_json(self)
                email = (data.get("email") or "").strip().lower()
                password = data.get("password") or ""
                row = find_user_by_email(email)

                if not row or not password_ok(password, row["password_hash"]):
                    send_json(
                        self,
                        {"error": "Неверный email или пароль."},
                        HTTPStatus.UNAUTHORIZED,
                    )
                    return

                token = create_session(row["id"])
                send_json(
                    self,
                    {"user": {"id": row["id"], "email": row["email"]}},
                    headers=set_session_headers(token),
                )
                return

            if path == "/api/auth/logout":
                delete_session(session_token(self))
                send_json(self, {"ok": True}, headers=clear_session_headers())
                return

            if path == "/api/recipes/import":
                user = require_user(self)
                if not user:
                    return

                meal_id = read_json(self).get("meal_id")
                if not meal_id:
                    send_json(
                        self, {"error": "Передайте meal_id."}, HTTPStatus.BAD_REQUEST
                    )
                    return

                recipe = meal_by_id(meal_id)
                if not recipe:
                    send_json(
                        self, {"error": "Рецепт не найден."}, HTTPStatus.NOT_FOUND
                    )
                    return

                send_json(
                    self, {"item": save_recipe(user["id"], recipe)}, HTTPStatus.CREATED
                )
                return

            if path in ("/api/ai/task", "/api/ai/question"):
                user = require_user(self)
                if not user:
                    return

                data = read_json(self)
                recipe_id = int(data.get("recipe_id") or 0)
                recipe = user_recipe(user["id"], recipe_id)
                if not recipe:
                    send_json(
                        self,
                        {"error": "Рецепт не найден в вашей базе."},
                        HTTPStatus.NOT_FOUND,
                    )
                    return

                if path == "/api/ai/task":
                    task = data.get("task") or ""
                    if task not in AI_TASKS:
                        send_json(
                            self,
                            {"error": "Неизвестная команда AI."},
                            HTTPStatus.BAD_REQUEST,
                        )
                        return
                    prompt = AI_TASKS[task]
                else:
                    prompt = (data.get("question") or "").strip()
                    if not prompt:
                        send_json(
                            self,
                            {"error": "Введите вопрос для Mistral."},
                            HTTPStatus.BAD_REQUEST,
                        )
                        return

                send_json(self, {"answer": ask_mistral(recipe, prompt)})
                return

            send_json(self, {"error": "Маршрут не найден."}, HTTPStatus.NOT_FOUND)
        except ValueError as error:
            send_json(self, {"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:  # noqa: BLE001
            send_json(self, {"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format_string, *args):
        """Короткий лог в терминале."""
        print(
            f"[{self.log_date_time_string()}] {self.address_string()} - {format_string % args}"
        )


def run():
    """Запуск приложения."""
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Server started on http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    run()
