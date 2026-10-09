import os
import json
import shlex
import subprocess
from pathlib import Path

from flask import Flask, jsonify, request, render_template
from anthropic import Anthropic

BASE_DIR = Path(os.environ.get("WORKSPACE_DIR", "./workspace")).resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)

# API credentials are provided from the trusted browser UI and are not persisted.

# Small allowlist. Commands run only inside the project directory.
ALLOWED_COMMANDS = {
    "pwd", "ls", "find", "cat", "head", "tail", "wc", "grep",
    "python", "python3", "node", "npm", "pytest", "git"
}
BLOCKED_TOKENS = {"sudo", "su", "shutdown", "reboot", "mkfs", "dd", "mount", "umount"}
MAX_OUTPUT = 12000
MAX_FILE_BYTES = 500_000


def safe_path(rel_path: str) -> Path:
    rel_path = (rel_path or "").strip()
    if not rel_path:
        raise ValueError("Укажи путь к файлу.")
    target = (BASE_DIR / rel_path).resolve()
    if target != BASE_DIR and BASE_DIR not in target.parents:
        raise ValueError("Путь выходит за пределы рабочей папки.")
    return target


def list_tree(path: Path, depth=0, max_depth=4):
    items = []
    if depth > max_depth:
        return items
    try:
        children = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except (PermissionError, OSError):
        return items
    for child in children:
        if child.name in {".git", "node_modules", "__pycache__", ".venv"}:
            continue
        rel = str(child.relative_to(BASE_DIR))
        entry = {"name": child.name, "path": rel, "type": "dir" if child.is_dir() else "file"}
        if child.is_dir():
            entry["children"] = list_tree(child, depth + 1, max_depth)
        items.append(entry)
    return items


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/tree")
def tree():
    return jsonify(list_tree(BASE_DIR))


@app.get("/api/file")
def read_file():
    try:
        path = safe_path(request.args.get("path", ""))
        if not path.is_file():
            return jsonify(error="Это не файл."), 400
        if path.stat().st_size > MAX_FILE_BYTES:
            return jsonify(error="Файл слишком большой для редактора."), 413
        return jsonify(path=str(path.relative_to(BASE_DIR)), content=path.read_text(encoding="utf-8"))
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/file")
def write_file():
    data = request.get_json(force=True)
    try:
        path = safe_path(data.get("path", ""))
        content = data.get("content", "")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return jsonify(error="Содержимое слишком большое."), 413
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return jsonify(ok=True, path=str(path.relative_to(BASE_DIR)))
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/mkdir")
def mkdir():
    data = request.get_json(force=True)
    try:
        path = safe_path(data.get("path", ""))
        path.mkdir(parents=True, exist_ok=True)
        return jsonify(ok=True)
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/command")
def command():
    data = request.get_json(force=True)
    raw = data.get("command", "")
    try:
        args = shlex.split(raw)
        if not args:
            return jsonify(error="Пустая команда."), 400
        if args[0] not in ALLOWED_COMMANDS:
            return jsonify(error=f"Команда '{args[0]}' не разрешена. Разрешены: {', '.join(sorted(ALLOWED_COMMANDS))}"), 403
        if any(tok in BLOCKED_TOKENS for tok in args):
            return jsonify(error="Команда содержит запрещённый токен."), 403
        # Avoid shell=True. cwd is restricted, but this is not a full OS sandbox.
        proc = subprocess.run(
            args, cwd=BASE_DIR, capture_output=True, text=True, timeout=15,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(BASE_DIR)}
        )
        output = (proc.stdout + ("\n" + proc.stderr if proc.stderr else ""))[:MAX_OUTPUT]
        return jsonify(returncode=proc.returncode, output=output)
    except subprocess.TimeoutExpired:
        return jsonify(error="Команда превысила лимит 15 секунд."), 408
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/chat")
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

    system = f"""Ты — coding-агент в стиле Claude Code. Рабочая папка: {BASE_DIR}.
Отвечай по-русски, если пользователь пишет по-русски.
Ты не можешь выполнять инструменты напрямую. Если нужно изменить файлы, верни JSON-блок
в формате:
{{"reply":"краткое объяснение","changes":[{{"path":"relative/path","content":"полное новое содержимое файла"}}]}}
Для обычного ответа используй обычный текст или JSON с reply и пустым changes.
Не предлагай пути за пределами рабочей папки. Не проси секреты и не выводи API-ключи.
Изменения будут применены только после подтверждения пользователя."""

    history_msgs = []
    for item in history[-12:]:
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            history_msgs.append({"role": role, "content": content[:12000]})

    try:
        if provider == "anthropic":
            from anthropic import Anthropic
            client = Anthropic(api_key=api_key, base_url=base_url or None)
            response = client.messages.create(
                model=model, max_tokens=5000, system=system,
                messages=history_msgs + [{"role": "user", "content": message[:12000]}]
            )
            result_text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        elif provider == "openai":
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url or None)
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}] + history_msgs + [{"role": "user", "content": message[:12000]}],
                max_tokens=5000
            )
            result_text = response.choices[0].message.content or ""
        else:
            return jsonify(error="Неизвестный провайдер API."), 400
        return jsonify(reply=result_text)
    except Exception as exc:
        return jsonify(error=f"Ошибка API ({provider}): {type(exc).__name__}: {str(exc)[:500]}"), 502


if __name__ == "__main__":
    # Bind locally by default; put behind authenticated HTTPS reverse proxy for remote access.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")), debug=False)
