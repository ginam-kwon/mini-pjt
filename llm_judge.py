# llm_judge.py - LLM-as-Judge 채점 (day7 llm_judge.py 스타일, RAGAS 대신 경량 구조화출력 채점)
from __future__ import annotations

from src.common import default_llm
from src.schemas import JudgeResult

JUDGE_MODEL_TEMPERATURE = 0


def judge(question: str, answer: str, expectation: str = "") -> JudgeResult:
    """질문/답변(+선택적 기대 근거)을 보고 1~5점과 정답 여부를 판정한다."""
    checker = default_llm(temperature=JUDGE_MODEL_TEMPERATURE).with_structured_output(JudgeResult)
    prompt = (
        "너는 Oracle SQL 성능 진단 Agent의 답변 품질을 채점하는 심사위원이다. "
        "질문의 의도에 맞게 답했는지, 근거 있는 원인/개선안을 제시했는지 기준으로 1~5점을 매겨라.\n\n"
        f"질문: {question}\n\n답변: {answer}\n\n"
        f"참고(있다면 답변이 다뤄야 할 핵심 내용): {expectation or '(없음)'}"
    )
    return checker.invoke(prompt)


if __name__ == "__main__":
    result = judge(
        question="풀 테이블 스캔이 왜 발생하나요?",
        answer="컬럼에 함수를 씌우면 인덱스를 못 써서 풀스캔이 발생합니다. 함수 기반 인덱스를 만드세요.",
        expectation="인덱스 부재 또는 함수로 인한 인덱스 미사용",
    )
    print(result)
