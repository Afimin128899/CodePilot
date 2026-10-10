import hmac
import json
import os
import secrets
import shlex
import subprocess
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

load_dotenv()

BASE_DIR = Path(os.environ.get("WORKSPACE_DIR", "./workspace")).resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET_KEY = os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}
ENABLE_COMMANDS = os.environ.get("ENABLE_COMMANDS", "0").lower() in {"1", "true", "yes"}
MAX_FILE_BYTES = 500_000
MAX_OUTPUT = 12_000
MAX_AGENT_STEPS = 8
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".pytest_cache"}
SENSITIVE_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

# Agent tools return proposed file changes; they never write files automatically.
TOOL_SCHEMAS = [
    {"name": "list_files", "description": "List files and folders in the workspace.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "read_file", "description": "Read a UTF-8 text file in the workspace.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "search_text", "description": "Search text in project files. Returns matching paths and line numbers.", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "propose_file_change", "description": "Propose a complete replacement or a new file. The user must approve it before it is saved.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "run_command", "description": "Run a narrowly allowlisted development command in the workspace. Commands are disabled unless ENABLE_COMMANDS=1.", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
]


def safe_path(rel_path: str) -> Path:
    rel_path = (rel_path or "").strip()
    if not rel_path or "\x00" in rel_path:
        raise ValueError("Укажи корректный относительный путь.")
    raw = Path(rel_path)
    if raw.is_absolute():
        raise ValueError("Нужен относительный путь внутри workspace.")
    target = (BASE_DIR / raw).resolve()
    if target != BASE_DIR and BASE_DIR not in target.parents:
        raise ValueError("Путь выходит за пределы рабочей папки.")
    if any(part in {"..", ".git"} for part in raw.parts):
        raise ValueError("Путь содержит запрещённый компонент.")
    return target


def is_sensitive_path(path: Path) -> bool:
    rel = path.relative_to(BASE_DIR)
    return any(part in SENSITIVE_NAMES or part.endswith((".pem", ".key", ".session")) for part in rel.parts)


def list_tree(path: Path, depth=0, max_depth=5):
    items = []
    if depth > max_depth:
        return items
    try:
        children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except (PermissionError, OSError):
        return items
    for child in children:
        if child.name in SKIP_DIRS or child.is_symlink():
            continue
        rel = str(child.relative_to(BASE_DIR))
        entry = {"name": child.name, "path": rel, "type": "dir" if child.is_dir() else "file"}
        if child.is_dir():
            entry["children"] = list_tree(child, depth + 1, max_depth)
        items.append(entry)
    return items


def validate_command(raw: str):
    if not ENABLE_COMMANDS:
        raise PermissionError("Выполнение команд отключено. Для изолированного Docker-запуска установи ENABLE_COMMANDS=1.")
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 400:
        raise ValueError("Пустая или слишком длинная команда.")
    # Never interpret shell syntax. The process is launched with shell=False.
    if any(ch in raw for ch in ";|&><\`$(){}\\\n\r"):
        raise ValueError("Shell-операторы и подстановки запрещены.")
    args = shlex.split(raw)
    if not args:
        raise ValueError("Пустая команда.")
    executable = args[0]
    if executable == "pwd" and len(args) == 1:
        return args
    if executable == "ls":
        allowed_flags = {"-a", "-l", "-la", "-al", "-lh", "-lah", "-alh"}
        rest = args[1:]
        paths = [x for x in rest if not x.startswith("-")]
        flags = [x for x in rest if x.startswith("-")]
        if any(x not in allowed_flags for x in flags) or len(paths) > 1:
            raise PermissionError("Разрешены только ls с флагами -a/-l и одним путём.")
        if paths:
            safe_path(paths[0])
        return args
    if args in (["python3", "-m", "compileall", "."], ["python", "-m", "compileall", "."]):
        return args
    # Tests execute project code, so enable them only inside the documented isolated deployment.
    if executable in {"pytest", "python3", "python"}:
        if executable in {"python3", "python"} and args[1:3] != ["-m", "pytest"]:
            raise PermissionError("Python разрешён только для compileall или pytest.")
        test_args = args[1:] if executable == "pytest" else args[3:]
        if executable != "pytest" and args[1:3] != ["-m", "pytest"]:
            raise PermissionError("Разрешены только тесты pytest.")
        for item in test_args:
            if item in {"-q", "-v", "-vv", "--tb=short", "--disable-warnings"}:
                continue
            if item.startswith("-"):
                raise PermissionError(f"Флаг {item} не разрешён.")
            safe_path(item)
        return args
    raise PermissionError(f"Команда {executable!r} не разрешена. Доступны pwd, ls, python3 -m compileall . и pytest.")


def run_command(raw: str):
    args = validate_command(raw)
    proc = subprocess.run(
        args, cwd=BASE_DIR, capture_output=True, text=True, timeout=30, shell=False,
        close_fds=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(BASE_DIR), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    output = (proc.stdout + (("\n" + proc.stderr) if proc.stderr else ""))[:MAX_OUTPUT]
    return {"returncode": proc.returncode, "output": output}


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify(error="Требуется вход в CodePilot."), 401
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped


@app.before_request
def security_checks():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(str(supplied), str(expected)):
            return jsonify(error="CSRF-проверка не пройдена. Обнови страницу и попробуй снова."), 400
    if request.endpoint in {"login", "static"} or request.endpoint is None:
        return None
    if not session.get("authenticated"):
        if request.path.startswith("/api/"):
            return jsonify(error="Требуется вход в CodePilot."), 401
        return redirect(url_for("login", next=request.path))
    return None


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    return response


@app.get("/login")
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))
    return render_template("login.html", csrf_token=session["csrf_token"], password_configured=bool(APP_PASSWORD and APP_PASSWORD != "replace-with-a-long-password"))


@app.post("/login")
def login_post():
    if not APP_PASSWORD or APP_PASSWORD == "replace-with-a-long-password":
        return render_template("login.html", csrf_token=session["csrf_token"], password_configured=False, error="Сначала задай собственный APP_PASSWORD в файле .env."), 503
    password = request.form.get("password", "")
    if not hmac.compare_digest(password, APP_PASSWORD):
        return render_template("login.html", csrf_token=session["csrf_token"], password_configured=True, error="Неверный пароль."), 401
    session.clear()
    session["authenticated"] = True
    session["csrf_token"] = secrets.token_urlsafe(32)
    return redirect(url_for("index"))


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def index():
    return render_template("index.html", csrf_token=session["csrf_token"], commands_enabled=ENABLE_COMMANDS)


@app.get("/api/tree")
@login_required
def tree():
    return jsonify(list_tree(BASE_DIR))


@app.get("/api/file")
@login_required
def read_file():
    try:
        path = safe_path(request.args.get("path", ""))
        if is_sensitive_path(path):
            return jsonify(error="Чтение секретных файлов запрещено."), 403
        if not path.is_file():
            return jsonify(error="Это не файл."), 400
        if path.stat().st_size > MAX_FILE_BYTES:
            return jsonify(error="Файл слишком большой для редактора."), 413
        return jsonify(path=str(path.relative_to(BASE_DIR)), content=path.read_text(encoding="utf-8"))
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/file")
@login_required
def write_file():
    data = request.get_json(force=True)
    try:
        path = safe_path(data.get("path", ""))
        if is_sensitive_path(path):
            return jsonify(error="Запись в секретные файлы запрещена."), 403
        content = data.get("content", "")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return jsonify(error="Содержимое слишком большое."), 413
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return jsonify(ok=True, path=str(path.relative_to(BASE_DIR)))
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/mkdir")
@login_required
def mkdir():
    data = request.get_json(force=True)
    try:
        path = safe_path(data.get("path", ""))
        path.mkdir(parents=True, exist_ok=True)
        return jsonify(ok=True)
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


def agent_tool(name, data, pending_changes):
    if name == "list_files":
        return {"files": list_tree(BASE_DIR)}
    if name == "read_file":
        path = safe_path(data.get("path", ""))
        if is_sensitive_path(path):
            raise PermissionError("Чтение секретных файлов запрещено.")
        if not path.is_file():
            raise ValueError("Файл не найден.")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Файл слишком большой.")
        return {"path": str(path.relative_to(BASE_DIR)), "content": path.read_text(encoding="utf-8")}
    if name == "search_text":
        query = str(data.get("query", "")).strip()
        if not query or len(query) > 200:
            raise ValueError("Запрос поиска должен содержать 1–200 символов.")
        matches = []
        for file_path in BASE_DIR.rglob("*"):
            if len(matches) >= 50:
                break
            if not file_path.is_file() or file_path.is_symlink() or any(p in SKIP_DIRS for p in file_path.parts):
                continue
            if is_sensitive_path(file_path) or file_path.stat().st_size > 200_000:
                continue
            try:
                for line_no, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
                    if query.casefold() in line.casefold():
                        matches.append({"path": str(file_path.relative_to(BASE_DIR)), "line": line_no, "text": line[:300]})
                        if len(matches) >= 50:
                            break
            except (UnicodeDecodeError, OSError):
                continue
        return {"matches": matches}
    if name == "propose_file_change":
        path = safe_path(data.get("path", ""))
        if is_sensitive_path(path):
            raise PermissionError("Изменение секретных файлов запрещено.")
        content = data.get("content", "")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("Содержимое файла слишком большое.")
        rel = str(path.relative_to(BASE_DIR))
        pending_changes[rel] = {"path": rel, "content": content}
        return {"staged": True, "path": rel, "bytes": len(content.encode("utf-8")), "message": "Изменение подготовлено, но ещё не записано. Пользователь должен подтвердить его."}
    if name == "run_command":
        return run_command(str(data.get("command", "")))
    raise ValueError("Неизвестный инструмент.")


def tool_result(name, args, pending_changes):
    try:
        return agent_tool(name, args or {}, pending_changes)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:500]}"}


def run_anthropic(api_key, model, base_url, system, history_msgs, user_message, pending_changes):
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key, base_url=base_url or None, timeout=60.0, max_retries=1)
    messages = history_msgs + [{"role": "user", "content": user_message}]
    final_text = ""
    for _ in range(MAX_AGENT_STEPS):
        response = client.messages.create(model=model, max_tokens=5000, system=system, tools=TOOL_SCHEMAS, messages=messages)
        assistant_content = []
        tool_uses = []
        for block in response.content:
            if getattr(block, "type", "") == "text":
                assistant_content.append({"type": "text", "text": block.text})
                final_text += block.text
            elif getattr(block, "type", "") == "tool_use":
                assistant_content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
                tool_uses.append(block)
        if not tool_uses:
            return final_text
        messages.append({"role": "assistant", "content": assistant_content})
        results = []
        for block in tool_uses:
            result = tool_result(block.name, block.input, pending_changes)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result, ensure_ascii=False)[:18000]})
        messages.append({"role": "user", "content": results})
    return final_text + "\n\nДостигнут лимит шагов агента. Проверь подготовленные изменения."


def run_openai(api_key, model, base_url, system, history_msgs, user_message, pending_changes):
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=60.0, max_retries=1)
    tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in TOOL_SCHEMAS]
    messages = [{"role": "system", "content": system}] + history_msgs + [{"role": "user", "content": user_message}]
    final_text = ""
    for _ in range(MAX_AGENT_STEPS):
        response = client.chat.completions.create(model=model, messages=messages, tools=tools, tool_choice="auto", max_tokens=5000)
        message = response.choices[0].message
        if not message.tool_calls:
            final_text = message.content or ""
            return final_text
        messages.append(message.model_dump(exclude_none=True))
        for call in message.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = tool_result(call.function.name, args, pending_changes)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)[:18000]})
    return final_text + "\n\nДостигнут лимит шагов агента. Проверь подготовленные изменения."


@app.post("/api/chat")
@login_required
def chat():
    data = request.get_json(force=True)
    message = (data.get("message") or "").strip()
    history = data.get("history") or []
    provider = (data.get("provider") or "anthropic").strip().lower()
    api_key = (data.get("api_key") or "").strip()
    model = (data.get("model") or "").strip()
    base_url = (data.get("base_url") or "").strip()
    if not message:
        return jsonify(error="Напиши сообщение."), 400
    if not api_key:
        return jsonify(error="Введи свой API-ключ в настройках модели."), 400
    if not model:
        return jsonify(error="Выбери или укажи модель."), 400
    active_file = str(data.get("active_file") or "")[:300]
    active_content = str(data.get("active_content") or "")[:20000]
    system = f"""Ты — CodePilot, агент разработки с доступом к инструментам чтения, поиска, запуска ограниченных команд и подготовки правок.
Рабочая папка: {BASE_DIR}. Отвечай на языке пользователя, обычно по-русски.
Сначала изучай структуру и существующий код инструментами list_files/read_file/search_text, прежде чем предлагать крупные изменения.
Для любых файловых правок обязательно используй propose_file_change с полным содержимым файла. Никогда не говори, что файл сохранён: правки только предложены, пока пользователь не нажмёт подтверждение.
Не выводи секреты. Не читай и не меняй .env, ключи, сессии или другие секретные файлы. Не предлагай пути вне workspace.
Выполняй команды только когда это действительно нужно. Если команды отключены, объясни, что нужно включить их только в изолированной среде.
Делай изменения согласованными между файлами и не выдумывай результаты тестов.
Текущий файл редактора: {active_file or "(не выбран)"}
Содержимое открытого файла (может быть обрезано):
{active_content or "(нет)"}
"""
    history_msgs = []
    if isinstance(history, list):
        for item in history[-12:]:
            role, content = item.get("role"), item.get("content")
            if role in {"user", "assistant"} and isinstance(content, str):
                history_msgs.append({"role": role, "content": content[:12000]})
    pending_changes = {}
    try:
        if provider == "anthropic":
            result_text = run_anthropic(api_key, model, base_url, system, history_msgs, message[:12000], pending_changes)
        elif provider == "openai":
            result_text = run_openai(api_key, model, base_url, system, history_msgs, message[:12000], pending_changes)
        else:
            return jsonify(error="Неизвестный провайдер API."), 400
        return jsonify(reply=json.dumps({"reply": result_text, "changes": list(pending_changes.values())}, ensure_ascii=False))
    except Exception as exc:
        app.logger.exception("Model API request failed")
        return jsonify(error=f"Ошибка API ({provider}): {type(exc).__name__}: {str(exc)[:300]}"), 502


@app.post("/api/command")
@login_required
def command():
    data = request.get_json(force=True)
    try:
        return jsonify(run_command(data.get("command", "")))
    except subprocess.TimeoutExpired:
        return jsonify(error="Команда превысила лимит 30 секунд."), 408
    except PermissionError as exc:
        return jsonify(error=str(exc)), 403
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


if __name__ == "__main__":
    if not APP_PASSWORD or APP_PASSWORD == "replace-with-a-long-password":
        app.logger.warning("Set a unique APP_PASSWORD in .env before using CodePilot.")
    if not os.environ.get("APP_SECRET_KEY"):
        app.logger.warning("APP_SECRET_KEY is not set; sessions will reset on restart.")
    # Local-only by default. Use Docker + authenticated HTTPS reverse proxy for remote access.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")), debug=False)
