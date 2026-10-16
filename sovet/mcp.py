"""Small stdio MCP server shared by both native CLI runtimes."""
import json
import sys

from .core import MEMORIES, ROLES, Project
from .integrations import comments, reply
from .research import run_lean, search, verification_status


def tool(name, description, properties=None, required=()):
    return {"name": name, "description": description,
            "annotations": {"readOnlyHint": name in {"memory_read", "events_read", "artifact_read", "chitalka_list", "lean_search", "lean_status"},
                            "destructiveHint": name == "artifact_write", "openWorldHint": False},
            "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required),
                            "additionalProperties": False}}


S = {"type": "string"}
TOOLS = [
    tool("ask_peer", "Поставить другому агенту задачу независимого ревью. Асинхронно: вернёт job_id; "
         "ответ придёт в общую историю и отдельным ходом. Не ждите его в цикле.", {"prompt": S}, ("prompt",)),
    tool("memory_read", "Текущая общая память и новые события"),
    tool("events_read", "Прочитать полные события общей истории после заданного ID",
         {"after": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}),
    tool("memory_append", "Добавить проверенное решение/результат в общую память",
         {"name": {"type": "string", "enum": list(MEMORIES)}, "text": S}, ("name", "text")),
    tool("artifact_read", "Прочитать редактируемый результат и SHA для защиты от устаревшей записи",
         {"path": S}, ("path",)),
    tool("artifact_write", "Записать результат, только если SHA совпадает с прочитанной версией. "
         "При конфликте перечитайте файл и сохраните изменения автора.",
         {"path": S, "text": S, "expected_sha": S}, ("path", "text", "expected_sha")),
    tool("chitalka_list", "Получить комментарии автора с точными местами в .tex"),
    tool("chitalka_reply", "Ответить в существующей карточке chitalka. Закрывать только после проверки результата.",
         {"id": S, "text": S, "done": {"type": "boolean"}}, ("id", "text")),
]

MATH_TOOLS = [
    tool("lean_status", "Проверить, относится ли последнее доказательство/аудит к текущим исходникам"),
    tool("lean_check", "Проверить .lean компилятором; вернуть диагностику, не считать это аудитом постановки",
         {"path": S}, ("path",)),
    tool("lean_build", "Собрать выбранный Lean-модуль с закреплёнными зависимостями",
         {"module": S}, ("module",)),
    tool("lean_search", "Найти определения и леммы в локальных LongTails и mathlib",
         {"query": S}, ("query",)),
    tool("longtails_audit", "Независимый Comparator-аудит публичной теоремы и oracle budget",
         {"theorem_file": S}, ("theorem_file",)),
]


def available_tools(project):
    tools = TOOLS[:]
    if project.config.get("profile") == "math-research":
        tools += MATH_TOOLS
    elif not project.config.get("reader"):
        tools = [t for t in tools if not t["name"].startswith("chitalka_")]
    return tools


def call(project, role, depth, read_only, name, args):
    if name not in {t["name"] for t in available_tools(project)}:
        raise ValueError(f"Инструмент не включён в текущем профиле: {name}")
    if name == "lean_status":
        return verification_status(project)
    if name == "lean_check":
        return run_lean(project, "check", args["path"])
    if name == "lean_build":
        return run_lean(project, "build", args["module"])
    if name == "lean_search":
        return search(project, args["query"])
    if name == "longtails_audit":
        return run_lean(project, "audit", args["theorem_file"])
    if name == "ask_peer":
        peer = next(r for r in ROLES if r != role)
        job_id = project.enqueue(peer, args["prompt"], origin=role, depth=depth + 1,
                                 read_only=True, reply_to=role)
        return {"job_id": job_id, "to": peer, "delivery": "queued; keep working or finish your turn"}
    if name == "memory_read":
        return {"context": project.context(role)[0]}
    if name == "events_read":
        return project.events(max(0, int(args.get("after", 0))), min(20, max(1, int(args.get("limit", 10)))))
    if name == "memory_append":
        project.memory_write(args["name"], args["text"], actor=role, append=True)
        return {"ok": True}
    if name == "artifact_read":
        return project.read_artifact(args["path"])
    if name == "artifact_write":
        return project.write_artifact(args["path"], args["text"], args["expected_sha"], actor=role, read_only=read_only)
    if name == "chitalka_list":
        return comments(project)
    if name == "chitalka_reply":
        if read_only or role != project.config["writer"]:
            raise ValueError("Ответы автору в chitalka отправляет назначенный исполнитель")
        return {"message": reply(project, args["id"], args["text"], args.get("done", False))}
    raise ValueError(f"Неизвестный инструмент: {name}")


def dispatch(project, role, depth, read_only, request):
    rid = request.get("id")
    method = request.get("method")
    if rid is None:
        return None
    response = {"jsonrpc": "2.0", "id": rid}
    if method == "initialize":
        response["result"] = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
                              "capabilities": {"tools": {}}, "serverInfo": {"name": "sovet", "version": "0.1.0"}}
    elif method == "tools/list":
        response["result"] = {"tools": available_tools(project)}
    elif method == "tools/call":
        params = request.get("params", {})
        try:
            value = call(project, role, depth, read_only, params.get("name"), params.get("arguments", {}))
            response["result"] = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
        except Exception as e:
            response["result"] = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    elif method in ("ping",):
        response["result"] = {}
    elif method in ("resources/list", "resources/templates/list", "prompts/list"):
        response["result"] = {"resources/list": {"resources": []}, "resources/templates/list": {"resourceTemplates": []},
                              "prompts/list": {"prompts": []}}[method]
    else:
        response["error"] = {"code": -32601, "message": f"Method not found: {method}"}
    return response


def serve(project: Project, role: str, depth=0, read_only=False):
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = dispatch(project, role, depth, read_only, request)
        except (ValueError, TypeError, AttributeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
