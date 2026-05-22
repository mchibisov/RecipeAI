from flask import Flask, jsonify, redirect, render_template, request

from core import (
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


app = Flask(__name__)


def json_body():
    return request.get_json(silent=True) or {}


def require_user():
    user = current_user(request.cookies.get("session_token"))
    if not user:
        return None, (jsonify({"error": "Please sign in first."}), 401)
    return user, None


def auth_response(user_id, email):
    response = jsonify({"user": {"id": user_id, "email": email}})
    response.set_cookie(
        "session_token",
        create_session(user_id),
        httponly=True,
        samesite="Lax",
    )
    return response


@app.get("/")
def index():
    if current_user(request.cookies.get("session_token")):
        return redirect("/recipes")
    return redirect("/auth")


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
    data = json_body()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if "@" not in email:
        return jsonify({"error": "Enter a valid email."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400

    user_id = create_user(email, password)
    if not user_id:
        return jsonify({"error": "Email is already registered."}), 409

    return auth_response(user_id, email)


@app.post("/api/auth/login")
def auth_login():
    data = json_body()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    user = user_by_email(email)

    if not user or not password_ok(password, user["password_hash"]):
        return jsonify({"error": "Wrong email or password."}), 401

    return auth_response(user["id"], user["email"])


@app.post("/api/auth/logout")
def auth_logout():
    delete_session(request.cookies.get("session_token"))
    response = jsonify({"ok": True})
    response.delete_cookie("session_token")
    return response


@app.get("/api/recipes/search")
def recipes_search():
    _, error = require_user()
    if error:
        return error

    query = request.args.get("q", "").strip()
    return jsonify({"items": search_meals(query) if query else []})


@app.post("/api/ai")
def ai_prompt():
    _, error = require_user()
    if error:
        return error

    data = json_body()
    recipe = data.get("recipe")
    question = (data.get("question") or "").strip()

    if not recipe:
        return jsonify({"error": "Choose a recipe."}), 400
    if not question:
        return jsonify({"error": "Enter a question."}), 400

    return jsonify({"answer": ask_mistral(recipe, question)})


def run():
    init_db()
    app.run(host="127.0.0.1", port=PORT, debug=False)


if __name__ == "__main__":
    run()
