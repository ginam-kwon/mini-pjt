# schemas.py - 구조화 출력 스키마 모음 (LCEL day1 패턴)
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RootCause(BaseModel):
    cause: str = Field(description="성능 저하 원인 (예: 풀 테이블 스캔, 카디널리티 오추정, 부적절한 조인 방식 등)")
    evidence: str = Field(description="실행계획에서 이 원인을 뒷받침하는 구체적 근거(연산자, Cost, Rows, 접근 경로 등)")
    severity: Literal["높음", "보통", "낮음"] = Field(description="심각도")


class Improvement(BaseModel):
    recommendation: str = Field(description="구체적인 개선 방안")
    target_cause: str = Field(description="이 개선안이 해결하려는 원인(RootCause.cause 중 하나)")
    expected_effect: str = Field(description="기대 효과 (예: Cost/Rows 감소, 인덱스 스캔 전환 등)")
    example: str = Field(default="", description="가능하면 예시 SQL/힌트/인덱스 DDL")
    risk_level: Literal["read", "write", "destructive"] = Field(
        default="read", description="이 개선안을 실행에 옮길 때의 위험도 (조회=read, 변경=write, 되돌리기 어려움=destructive)"
    )


class SqlPlanAnalysis(BaseModel):
    summary: str = Field(description="전체 진단 한 줄 요약")
    root_causes: list[RootCause]
    improvements: list[Improvement]


class Plan(BaseModel):
    steps: list[str] = Field(description="이 진단 요청을 처리하기 위한 순서있는 단계 목록")


class Replan(BaseModel):
    """다음 행동: 계획대로 계속 실행하거나(steps), 이미 답이 나왔으면 종료(response)."""

    steps: list[str] = Field(default_factory=list, description="남은 단계. 비어 있으면 response로 종료")
    response: str = Field(default="", description="남은 단계가 없을 때의 최종 응답 요약")


class InjectionCheck(BaseModel):
    is_injection: bool = Field(description="프롬프트 인젝션/역할 탈취/시스템 프롬프트 유출 시도 여부")
    confidence: float = Field(description="0~1 확신도")
    reason: str = Field(description="판단 근거 한 줄")


class JudgeResult(BaseModel):
    score: int = Field(description="1~5 점수")
    is_correct: bool = Field(description="기대 정답/의도에 부합하는지")
    missing: str = Field(default="", description="빠진 내용")
    reasoning: str = Field(description="채점 근거")
