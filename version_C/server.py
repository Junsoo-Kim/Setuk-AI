from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template_string, request, url_for
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .bridge import ROOT
from .pipeline import build_graph

load_dotenv(Path(__file__).resolve().parent / ".env")

app = Flask(__name__)

_DB_PATH = Path(__file__).resolve().parent / "run_state_c.sqlite3"
_checkpointer_cm = SqliteSaver.from_conn_string(str(_DB_PATH))
_checkpointer = _checkpointer_cm.__enter__()
_graph = build_graph(_checkpointer)

_RUNS_LOCK = threading.Lock()
_RUNS: dict[str, dict[str, Any]] = {}


def _apply_result(run_id: str, state: dict[str, Any]) -> None:
    with _RUNS_LOCK:
        info = _RUNS[run_id]
        interrupts = state.get("__interrupt__")
        if interrupts:
            payload = interrupts[0].value
            info["status"] = "awaiting_review"
            info["draft_text"] = payload.get("draft_text", "")
        elif state.get("error"):
            info["status"] = "error"
            info["error"] = state["error"]
        elif state.get("lint_passed"):
            info["status"] = "done"
            info["output_path"] = state.get("output_path")
            info["lint_summary"] = state.get("lint_summary")
        else:
            info["status"] = "lint_failed"
            info["lint_summary"] = state.get("lint_summary")
            info["lint_retry_count"] = state.get("lint_retry_count")


def _run_graph(run_id: str, payload: dict[str, Any], resume: bool) -> None:
    config = {"configurable": {"thread_id": run_id}}
    try:
        if resume:
            state = _graph.invoke(Command(resume=payload), config=config)
        else:
            state = _graph.invoke(payload, config=config)
    except Exception as exc:
        with _RUNS_LOCK:
            _RUNS[run_id]["status"] = "error"
            _RUNS[run_id]["error"] = f"{type(exc).__name__}: {exc}"
        return
    _apply_result(run_id, state)


@app.route("/", methods=["GET"])
def index():
    with _RUNS_LOCK:
        runs = dict(_RUNS)
    return render_template_string(INDEX_TEMPLATE, runs=runs)


@app.route("/start", methods=["POST"])
def start():
    student_key = request.form.get("student_key", "").strip()
    mode = request.form.get("mode", "")
    if not student_key or mode not in ("docx", "yaml"):
        abort(400, "학생 식별자와 입력 방식을 확인하세요.")

    run_id = f"{student_key}-{uuid.uuid4().hex[:8]}"
    payload: dict[str, Any] = {"student_key": student_key, "mode": mode}
    if mode == "docx":
        payload["report_path"] = request.form.get("report_path", "").strip()
    else:
        payload["yaml_path"] = request.form.get("yaml_path", "").strip()

    with _RUNS_LOCK:
        _RUNS[run_id] = {"student_key": student_key, "status": "running"}

    threading.Thread(target=_run_graph, args=(run_id, payload, False), daemon=True).start()
    return redirect(url_for("run_detail", run_id=run_id))


@app.route("/runs/<run_id>", methods=["GET"])
def run_detail(run_id: str):
    with _RUNS_LOCK:
        info = _RUNS.get(run_id)
    if info is None:
        abort(404, "알 수 없는 작업입니다.")
    return render_template_string(RUN_TEMPLATE, run_id=run_id, info=info)


@app.route("/runs/<run_id>/review", methods=["POST"])
def review(run_id: str):
    with _RUNS_LOCK:
        info = _RUNS.get(run_id)
        if info is None:
            abort(404, "알 수 없는 작업입니다.")
        if info.get("status") != "awaiting_review":
            abort(409, "지금은 검토할 수 있는 상태가 아닙니다.")
        info["status"] = "running"

    action = request.form.get("action")
    if action == "approve":
        payload: dict[str, Any] = {
            "approved": True,
            "edited_text": request.form.get("edited_text", ""),
        }
    elif action == "reject":
        payload = {"approved": False, "reason": request.form.get("reason", "").strip()}
    else:
        abort(400, "action은 approve 또는 reject여야 합니다.")

    threading.Thread(target=_run_graph, args=(run_id, payload, True), daemon=True).start()
    return redirect(url_for("run_detail", run_id=run_id))


_STYLE = """
<style>
  body { font-family: -apple-system, "Segoe UI", sans-serif; max-width: 760px; margin: 40px auto; color: #1a1a1a; line-height: 1.6; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
  textarea { width: 100%; box-sizing: border-box; font-size: 0.95rem; padding: 8px; }
  input[type=text] { width: 100%; box-sizing: border-box; padding: 6px; margin-bottom: 8px; }
  button { padding: 8px 16px; margin-right: 8px; cursor: pointer; }
  .status { display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 0.85rem; }
  .status-running { background: #fff3cd; } .status-awaiting_review { background: #cfe2ff; }
  .status-done { background: #d1e7dd; } .status-error, .status-lint_failed { background: #f8d7da; }
  table { border-collapse: collapse; width: 100%; } td, th { border-bottom: 1px solid #ddd; padding: 6px; text-align: left; }
  .warn { color: #842029; background: #f8d7da; padding: 10px; border-radius: 6px; }
  fieldset { margin-top: 1rem; }
</style>
"""

INDEX_TEMPLATE = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>Setuk-AI C버전</title>""" + _STYLE + """
</head><body>
<h1>Setuk-AI C버전 — 자율 멀티에이전트 (Claude API 직접 호출)</h1>
<p>이 버전은 무설치가 아니며 API 사용량만큼 비용이 발생합니다. 실제 학생 데이터로 쓰기 전에
   <code>학생정보/example.yaml</code> 같은 가상 사례로 먼저 흐름을 확인하세요.</p>

<h2>새 작업 시작</h2>
<form method="post" action="/start">
  <label>학생 식별자 (파일명에 쓸 값)</label>
  <input type="text" name="student_key" required placeholder="예: 김준수">
  <label>입력 방식</label><br>
  <label><input type="radio" name="mode" value="yaml" checked> 기존 YAML 사용</label>
  <input type="text" name="yaml_path" placeholder="예: 학생정보/김준수.yaml"><br>
  <label><input type="radio" name="mode" value="docx"> DOCX 보고서에서 자동 생성</label>
  <input type="text" name="report_path" placeholder="예: 보고서/김준수_주제탐구보고서.docx">
  <button type="submit">시작</button>
</form>

<h2>진행 중인 작업</h2>
{% if not runs %}<p>아직 시작한 작업이 없습니다.</p>{% endif %}
<table>
<tr><th>학생</th><th>상태</th><th></th></tr>
{% for run_id, info in runs.items() %}
<tr>
  <td>{{ info.student_key }}</td>
  <td><span class="status status-{{ info.status }}">{{ info.status }}</span></td>
  <td><a href="/runs/{{ run_id }}">열기</a></td>
</tr>
{% endfor %}
</table>
</body></html>
"""

RUN_TEMPLATE = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{{ info.student_key }} - Setuk-AI C버전</title>
{% if info.status in ("running",) %}<meta http-equiv="refresh" content="3">{% endif %}
""" + _STYLE + """
</head><body>
<p><a href="/">&larr; 목록으로</a></p>
<h1>{{ info.student_key }} <span class="status status-{{ info.status }}">{{ info.status }}</span></h1>

{% if info.status == "running" %}
  <p>AI가 처리 중입니다. 이 페이지는 3초마다 자동으로 새로고침됩니다.</p>

{% elif info.status == "awaiting_review" %}
  <h2>초안 검토</h2>
  <p>AI가 작성한 초안입니다. 필요하면 아래 내용을 직접 고친 뒤 승인하세요.
     사실 자체(활동 내용·수치·결과)를 새로 만들어 넣지 마세요 — 사실 관계를 바꾸려면
     반려 사유로 남기고 다시 작성시키는 편이 안전합니다.</p>
  <form method="post" action="/runs/{{ run_id }}/review">
    <input type="hidden" name="action" value="approve">
    <textarea name="edited_text" rows="10">{{ info.draft_text }}</textarea>
    <button type="submit">이 내용으로 승인</button>
  </form>
  <fieldset>
    <legend>반려하고 다시 작성시키기</legend>
    <form method="post" action="/runs/{{ run_id }}/review">
      <input type="hidden" name="action" value="reject">
      <input type="text" name="reason" placeholder="반려 사유 (예: 탐구 과정을 더 구체적으로)" required>
      <button type="submit">반려</button>
    </form>
  </fieldset>

{% elif info.status == "done" %}
  <p>완료되었습니다. Linter 오류 0건입니다.</p>
  <p>저장 경로: <code>{{ info.output_path }}</code></p>
  <pre>{{ info.lint_summary }}</pre>
  <p>본문을 검토한 뒤 NEIS에 복사하세요. 이 결과가 최신 학교생활기록부 기재요령을 자동으로
     보증하지 않습니다 — 최종 확인 책임은 교사에게 있습니다.</p>

{% elif info.status == "lint_failed" %}
  <div class="warn">
    <p>Linter를 {{ info.lint_retry_count }}회 재시도했지만 통과하지 못해 자동 수정을 멈췄습니다.
       아래 결과를 보고 <code>세특/{{ info.student_key }}.md</code>를 직접 확인·수정하세요.</p>
  </div>
  <pre>{{ info.lint_summary }}</pre>

{% elif info.status == "error" %}
  <div class="warn"><p>오류가 발생했습니다: {{ info.error }}</p></div>

{% endif %}
</body></html>
"""
