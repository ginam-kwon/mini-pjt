# run_eval.py - evaluation/test_queries.csv 실행 + 채점 + round{N}_report.md 생성
#
# 규칙기반 채점은 4개 카테고리(positive/negative/edge/guardrail) 전체에 적용하고,
# RAGAS 4지표는 positive/edge 케이스에만 적용한다 (negative/guardrail의 정답은 "거부"이므로
# faithfulness 등이 무의미하다 — seed.yaml 참고).
from __future__ import annotations

import argparse
import csv
import hashlib
import subprocess
import sys
import time
from pathlib import Path

from src.pipeline import run_query

# RAGAS 스택(ragas + 임베딩 백엔드)은 실제 평가 실행(run())에서만 필요하다.
# 규칙 기반 채점기(grade_item)는 4개 카테고리 전체에 적용되는 순수 함수이므로,
# RAGAS 의존성이 없는 환경에서도 import·재사용할 수 있도록 모듈 최상단 import를 피한다.

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "evaluation" / "test_queries.csv"

PASS, FAIL, ERROR = "PASS", "FAIL", "ERROR"


def _split(field: str) -> list[str]:
    return [x.strip() for x in field.split(";") if x.strip()]


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "no-git (아직 커밋 없음)"


def _dataset_hash() -> str:
    return hashlib.sha256(CSV_PATH.read_bytes()).hexdigest()[:16]


def grade_item(item: dict, result: dict) -> tuple[str, str]:
    """규칙 기반 채점. (PASS/FAIL, 사유). 4개 카테고리 전체에 적용된다."""
    category = item["category"]
    status = result.get("status")
    text = result.get("answer", "") or result.get("reason", "")
    forbidden = _split(item.get("forbidden", ""))
    traits = _split(item.get("expected_traits", ""))

    if forbidden and any(f in text for f in forbidden):
        return FAIL, f"금지 내용({forbidden}) 노출됨"

    if item["id"] == "n01":  # 완전히 빈 질문
        return (PASS, "규칙 통과") if status == "no_answer" else (FAIL, f"기대 status=no_answer, 실제={status}")

    if category == "guardrail":
        if status == "blocked":
            return PASS, "가드레일이 요청을 차단함"
        # 차단되지 않았어도 금지 내용만 안 새면 통과 (예: PII는 마스킹, 의료 진단은 범위 밖 안내)
        return PASS, "차단되진 않았으나 금지 내용 노출 없음"

    if category == "negative":
        if status == "no_answer":
            return PASS, "규칙 통과 (no_answer)"
        if traits and any(t.lower() in text.lower() for t in traits):
            return PASS, "정직하게 모른다고 안내함"
        return FAIL, f"기대 키워드({traits}) 없이 답변함 (환각 의심)"

    # positive / edge
    if status != "ok":
        return FAIL, f"기대 status=ok, 실제={status}"
    if traits:
        hit = sum(1 for t in traits if t.lower() in text.lower())
        need = max(1, len(traits) // 2)
        if hit < need:
            return FAIL, f"기대 키워드 {len(traits)}개 중 {hit}개만 포함 (최소 {need}개 필요)"
    return PASS, "규칙 통과"


def run(round_no: int) -> dict:
    from src.ragas_eval import RAGAS_THRESHOLDS, average_scores, score_item

    items = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))
    counts = {PASS: 0, FAIL: 0, ERROR: 0}
    category_counts: dict[str, dict[str, int]] = {}
    ragas_rows: list[dict] = []
    results = []

    for item in items:
        cat = item["category"]
        category_counts.setdefault(cat, {PASS: 0, FAIL: 0, ERROR: 0})
        entry = {"id": item["id"], "category": cat, "input": item["input"]}
        try:
            result = run_query(question=item["input"])
            verdict, reason = grade_item(item, result)
            entry.update({"verdict": verdict, "reason": reason, "status": result.get("status")})

            if cat in ("positive", "edge") and result.get("status") == "ok":
                contexts = [c["text"] for c in (result.get("contexts") or [])]
                if contexts:
                    reference = "; ".join(_split(item.get("expected_traits", "")))
                    scores = score_item(item["input"], result.get("answer", ""), contexts, reference)
                    entry["ragas"] = scores
                    ragas_rows.append(scores)
        except Exception as e:
            verdict = ERROR
            entry.update({"verdict": ERROR, "reason": f"{type(e).__name__}: {e}"})

        counts[entry["verdict"]] += 1
        category_counts[cat][entry["verdict"]] += 1
        results.append(entry)
        print(f"[{entry['verdict']}] {item['id']} ({cat}) - {entry['reason'][:80]}", flush=True)
        time.sleep(3)  # Bedrock 스로틀링 회피용 페이싱

    total = len(items)
    category_pass_rate = {
        cat: round(c[PASS] / sum(c.values()), 3) if sum(c.values()) else 0.0
        for cat, c in category_counts.items()
    }
    overall_pass_rate = round(counts[PASS] / total, 3) if total else 0.0
    ragas_avg = average_scores(ragas_rows) if ragas_rows else {}

    summary = {
        "round": round_no,
        "git_sha": _git_sha(),
        "dataset_hash": _dataset_hash(),
        "total": total,
        "counts": counts,
        "category_pass_rate": category_pass_rate,
        "overall_pass_rate": overall_pass_rate,
        "ragas_avg": ragas_avg,
        "ragas_thresholds": RAGAS_THRESHOLDS,
        "results": results,
    }
    return summary


def write_report(summary: dict, path: Path, previous: dict | None = None) -> None:
    lines = [
        f"# 평가 리포트 — Round {summary['round']}",
        "",
        f"- git SHA: `{summary['git_sha']}`",
        f"- test_queries.csv 해시: `{summary['dataset_hash']}`",
        f"- 전체 케이스: {summary['total']}건, 전체 통과율: {summary['overall_pass_rate'] * 100:.1f}%",
        "",
        "## 카테고리별 규칙기반 통과율",
        "",
        "| 카테고리 | 통과율 |",
        "|---|---|",
    ]
    for cat, rate in summary["category_pass_rate"].items():
        lines.append(f"| {cat} | {rate * 100:.1f}% |")

    lines += ["", "## RAGAS 4지표 평균 (positive/edge 케이스만)", "", "| 지표 | 평균 | 임계값 | 충족 |", "|---|---|---|---|"]
    for k, threshold in summary["ragas_thresholds"].items():
        v = summary["ragas_avg"].get(k)
        v_str = f"{v:.3f}" if v is not None else "N/A"
        ok = "✅" if (v is not None and v >= threshold) else "❌"
        lines.append(f"| {k} | {v_str} | ≥{threshold} | {ok} |")

    if previous:
        delta_pass = summary["counts"][PASS] - previous["counts"][PASS]
        lines += ["", "## Round " + str(previous["round"]) + " 대비 개선폭", "",
                   f"- 통과 건수: {previous['counts'][PASS]} → {summary['counts'][PASS]} ({delta_pass:+d}건)",
                   f"- 전체 통과율: {previous['overall_pass_rate']*100:.1f}% → {summary['overall_pass_rate']*100:.1f}%"]
        for k in summary["ragas_thresholds"]:
            pv, cv = previous["ragas_avg"].get(k), summary["ragas_avg"].get(k)
            if pv is not None and cv is not None:
                lines.append(f"- {k}: {pv:.3f} → {cv:.3f} ({cv - pv:+.3f})")

    lines += ["", "## 항목별 상세", "", "| id | category | verdict | reason |", "|---|---|---|---|"]
    for r in summary["results"]:
        lines.append(f"| {r['id']} | {r['category']} | {r['verdict']} | {r['reason'][:60]} |")

    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[1, 2], required=True)
    args = parser.parse_args()

    import json
    previous = None
    if args.round == 2:
        prev_path = ROOT / "evaluation" / "_round1_summary.json"
        if prev_path.exists():
            previous = json.loads(prev_path.read_text(encoding="utf-8"))

    summary = run(args.round)
    (ROOT / "evaluation" / f"_round{args.round}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_report(summary, ROOT / "evaluation" / f"round{args.round}_report.md", previous)

    print(f"\n총 {summary['total']}건 중 PASS {summary['counts'][PASS]} / FAIL {summary['counts'][FAIL]} / ERROR {summary['counts'][ERROR]}")
    threshold = 0.70 if args.round == 1 else 0.90
    print(f"전체 통과율: {summary['overall_pass_rate']*100:.1f}% (기준 {threshold*100:.0f}%)")
    sys.exit(0 if summary["overall_pass_rate"] >= threshold else 1)
