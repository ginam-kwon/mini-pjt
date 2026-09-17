"""AC 11: 현재 요청 상태는 checkpoints.sqlite에 저장되고
검증 이력, SQL 버전, 성능 기준선, 비즈니스 용어, 피드백은 memory.sqlite에 영속 저장된다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# import sanity
# ---------------------------------------------------------------------------

class TestStorageModuleImportable:
    def test_storage_module_importable(self):
        import src.storage  # noqa: F401
        assert src.storage is not None

    def test_checkpoint_store_class_exists(self):
        from src.storage import CheckpointStore
        assert CheckpointStore is not None

    def test_memory_store_class_exists(self):
        from src.storage import MemoryStore
        assert MemoryStore is not None

    def test_default_db_path_constants_defined(self):
        from src.storage import CHECKPOINTS_DB, MEMORY_DB
        assert "checkpoints.sqlite" in str(CHECKPOINTS_DB)
        assert "memory.sqlite" in str(MEMORY_DB)


# ---------------------------------------------------------------------------
# CheckpointStore — checkpoints.sqlite
# ---------------------------------------------------------------------------

class TestCheckpointStore:
    """checkpoints.sqlite에 현재 세션 요청 상태가 저장된다."""

    @pytest.fixture
    def store(self, tmp_path):
        from src.storage import CheckpointStore
        return CheckpointStore(db_path=tmp_path / "checkpoints.sqlite")

    def test_sqlite_file_created_on_init(self, tmp_path):
        from src.storage import CheckpointStore
        db = tmp_path / "checkpoints.sqlite"
        CheckpointStore(db_path=db)
        assert db.exists()

    def test_save_and_load_checkpoint(self, store):
        state = {"candidates": ["sql_001", "sql_002"], "step": "candidate_selection"}
        store.save("sess-001", "candidate_selection", state)
        loaded = store.load("sess-001", "candidate_selection")
        assert loaded == state

    def test_save_multiple_steps_same_session(self, store):
        store.save("sess-001", "planning", {"plan": ["step_a", "step_b"]})
        store.save("sess-001", "selection", {"selected": "sql_003"})
        assert store.load("sess-001", "planning") == {"plan": ["step_a", "step_b"]}
        assert store.load("sess-001", "selection") == {"selected": "sql_003"}

    def test_load_nonexistent_returns_none(self, store):
        assert store.load("no-sess", "no-step") is None

    def test_overwrite_checkpoint_same_step(self, store):
        store.save("sess-001", "step1", {"v": 1})
        store.save("sess-001", "step1", {"v": 2})
        assert store.load("sess-001", "step1") == {"v": 2}

    def test_load_latest_returns_most_recent(self, store):
        store.save("sess-001", "step_a", {"x": 1})
        store.save("sess-001", "step_b", {"x": 2})
        latest = store.load_latest("sess-001")
        assert latest is not None
        assert "step" in latest
        assert "state" in latest

    def test_load_latest_nonexistent_session_returns_none(self, store):
        assert store.load_latest("no-such-session") is None

    def test_list_steps_returns_all_steps(self, store):
        store.save("sess-001", "step_a", {})
        store.save("sess-001", "step_b", {})
        steps = store.list_steps("sess-001")
        assert "step_a" in steps
        assert "step_b" in steps

    def test_list_steps_empty_for_unknown_session(self, store):
        assert store.list_steps("ghost-session") == []

    def test_candidate_state_round_trip(self, store):
        """후보 목록 상태를 저장하고 복원할 수 있다."""
        candidates = [
            {"sql_id": "sql_001", "masked_sql": "SELECT * FROM orders WHERE id = :id", "score": 0.95},
            {"sql_id": "sql_002", "masked_sql": "SELECT COUNT(*) FROM payments", "score": 0.80},
        ]
        store.save("sess-A", "candidates", {"candidates": candidates})
        recovered = store.load("sess-A", "candidates")
        assert recovered["candidates"] == candidates

    def test_plan_stage_round_trip(self, store):
        """계획 단계를 저장하고 복원할 수 있다."""
        plan = {"steps": ["실행계획 조회", "RAG 지식 검색", "개선안 작성"], "current": 0}
        store.save("sess-B", "planning", plan)
        assert store.load("sess-B", "planning") == plan


# ---------------------------------------------------------------------------
# MemoryStore — memory.sqlite
# ---------------------------------------------------------------------------

class TestMemoryStore:
    """memory.sqlite에 세션 간 장기 기억이 영속 저장된다."""

    @pytest.fixture
    def store(self, tmp_path):
        from src.storage import MemoryStore
        return MemoryStore(db_path=tmp_path / "memory.sqlite")

    def test_sqlite_file_created_on_init(self, tmp_path):
        from src.storage import MemoryStore
        db = tmp_path / "memory.sqlite"
        MemoryStore(db_path=db)
        assert db.exists()

    # --- verification_history ---

    def test_save_and_get_verification(self, store):
        row_id = store.save_verification("fp001", "accept", query_name="Q1", review_id="rev-1")
        assert isinstance(row_id, int) and row_id > 0
        history = store.get_verification_history("fp001")
        assert len(history) == 1
        assert history[0]["result"] == "accept"
        assert history[0]["query_name"] == "Q1"
        assert history[0]["review_id"] == "rev-1"

    def test_verification_result_values(self, store):
        """accept / revise / reject 세 결과값 모두 저장된다."""
        for result in ("accept", "revise", "reject"):
            store.save_verification(f"fp_{result}", result)
        for result in ("accept", "revise", "reject"):
            h = store.get_verification_history(f"fp_{result}")
            assert h[0]["result"] == result

    def test_multiple_verifications_same_fingerprint(self, store):
        store.save_verification("fp002", "revise")
        store.save_verification("fp002", "accept")
        history = store.get_verification_history("fp002")
        assert len(history) == 2

    def test_verification_history_empty_for_unknown(self, store):
        assert store.get_verification_history("unknown_fp") == []

    def test_verification_reviewed_at_populated(self, store):
        store.save_verification("fp003", "accept")
        h = store.get_verification_history("fp003")
        assert h[0]["reviewed_at"]  # ISO timestamp not empty

    # --- sql_versions ---

    def test_save_and_get_sql_version(self, store):
        store.save_sql_version("fp001", "SELECT * FROM orders WHERE id = :id")
        versions = store.get_sql_versions("fp001")
        assert len(versions) == 1
        assert versions[0]["version"] == 1
        assert "SELECT" in versions[0]["masked_sql"]

    def test_sql_version_auto_increments(self, store):
        store.save_sql_version("fp001", "SELECT v1")
        store.save_sql_version("fp001", "SELECT v2")
        versions = store.get_sql_versions("fp001")
        assert len(versions) == 2
        assert versions[0]["version"] == 1
        assert versions[1]["version"] == 2

    def test_sql_versions_empty_for_unknown(self, store):
        assert store.get_sql_versions("no-fp") == []

    # --- performance_baselines ---

    def test_save_and_get_performance_baseline(self, store):
        store.save_performance_baseline(
            "fp001",
            plan_hash="abc123",
            cost_estimate=1500.0,
            rows_estimate=500,
            baseline_plan="HASH JOIN | TABLE ACCESS FULL",
        )
        baseline = store.get_performance_baseline("fp001")
        assert baseline is not None
        assert baseline["plan_hash"] == "abc123"
        assert baseline["cost_estimate"] == 1500.0
        assert baseline["rows_estimate"] == 500
        assert baseline["baseline_plan"] == "HASH JOIN | TABLE ACCESS FULL"

    def test_performance_baseline_upsert(self, store):
        store.save_performance_baseline("fp001", plan_hash="v1", cost_estimate=100.0)
        store.save_performance_baseline("fp001", plan_hash="v2", cost_estimate=200.0)
        baseline = store.get_performance_baseline("fp001")
        assert baseline["plan_hash"] == "v2"

    def test_performance_baseline_not_found(self, store):
        assert store.get_performance_baseline("nonexistent") is None

    def test_performance_baseline_captured_at_populated(self, store):
        store.save_performance_baseline("fp001")
        baseline = store.get_performance_baseline("fp001")
        assert baseline["captured_at"]

    # --- business_terms ---

    def test_save_and_get_business_term(self, store):
        store.save_business_term("주문번호", "고객이 주문할 때 발급되는 고유 번호", domain="주문")
        term = store.get_business_term("주문번호")
        assert term is not None
        assert term["definition"] == "고객이 주문할 때 발급되는 고유 번호"
        assert term["domain"] == "주문"

    def test_business_term_upsert(self, store):
        store.save_business_term("결제금액", "v1 정의")
        store.save_business_term("결제금액", "v2 정의")
        term = store.get_business_term("결제금액")
        assert term["definition"] == "v2 정의"

    def test_business_term_not_found(self, store):
        assert store.get_business_term("없는용어") is None

    def test_business_term_created_at_populated(self, store):
        store.save_business_term("테스트용어", "정의")
        term = store.get_business_term("테스트용어")
        assert term["created_at"]

    # --- feedback ---

    def test_save_and_get_feedback_by_fingerprint(self, store):
        row_id = store.save_feedback(
            "positive", "인덱스 제안이 효과적이었습니다.", sql_fingerprint="fp001"
        )
        assert isinstance(row_id, int) and row_id > 0
        feedback = store.get_feedback(sql_fingerprint="fp001")
        assert len(feedback) == 1
        assert feedback[0]["feedback_type"] == "positive"
        assert feedback[0]["content"] == "인덱스 제안이 효과적이었습니다."

    def test_save_and_get_feedback_by_session(self, store):
        store.save_feedback("correction", "튜닝안이 잘못됨", session_id="sess-123")
        feedback = store.get_feedback(session_id="sess-123")
        assert len(feedback) == 1
        assert feedback[0]["content"] == "튜닝안이 잘못됨"

    def test_feedback_type_values(self, store):
        """positive / negative / correction 세 타입이 저장된다."""
        for ftype in ("positive", "negative", "correction"):
            store.save_feedback(ftype, f"{ftype} content", sql_fingerprint=f"fp_{ftype}")
        for ftype in ("positive", "negative", "correction"):
            f = store.get_feedback(sql_fingerprint=f"fp_{ftype}")
            assert f[0]["feedback_type"] == ftype

    def test_get_feedback_no_filter_returns_empty(self, store):
        store.save_feedback("positive", "some feedback", sql_fingerprint="fp001")
        assert store.get_feedback() == []

    def test_feedback_created_at_populated(self, store):
        store.save_feedback("negative", "content", sql_fingerprint="fp001")
        feedback = store.get_feedback(sql_fingerprint="fp001")
        assert feedback[0]["created_at"]


# ---------------------------------------------------------------------------
# 영속성(durability) 검증: 인스턴스를 새로 열어도 데이터가 남아 있어야 한다
# ---------------------------------------------------------------------------

class TestSQLiteDurability:
    """SQLite 파일을 닫고 새 인스턴스로 열어도 데이터가 보존된다."""

    def test_checkpoint_persists_across_instances(self, tmp_path):
        from src.storage import CheckpointStore
        db = tmp_path / "checkpoints.sqlite"
        CheckpointStore(db_path=db).save("s1", "step1", {"durable": True})
        assert CheckpointStore(db_path=db).load("s1", "step1") == {"durable": True}

    def test_memory_term_persists_across_instances(self, tmp_path):
        from src.storage import MemoryStore
        db = tmp_path / "memory.sqlite"
        MemoryStore(db_path=db).save_business_term("영속", "재시작 후에도 살아남는 정의")
        term = MemoryStore(db_path=db).get_business_term("영속")
        assert term is not None
        assert term["definition"] == "재시작 후에도 살아남는 정의"

    def test_verification_history_persists_across_instances(self, tmp_path):
        from src.storage import MemoryStore
        db = tmp_path / "memory.sqlite"
        MemoryStore(db_path=db).save_verification("fpX", "accept", query_name="DurableQ")
        history = MemoryStore(db_path=db).get_verification_history("fpX")
        assert len(history) == 1
        assert history[0]["query_name"] == "DurableQ"

    def test_performance_baseline_persists_across_instances(self, tmp_path):
        from src.storage import MemoryStore
        db = tmp_path / "memory.sqlite"
        MemoryStore(db_path=db).save_performance_baseline("fpY", plan_hash="hash_dur", cost_estimate=42.0)
        baseline = MemoryStore(db_path=db).get_performance_baseline("fpY")
        assert baseline["plan_hash"] == "hash_dur"


# ---------------------------------------------------------------------------
# 저장소 분리: 세션 문맥(checkpoints.sqlite)과 장기기억(memory.sqlite)은 서로 다른 파일
# ---------------------------------------------------------------------------

class TestStoreSeparation:
    """현재 세션 상태와 세션 간 장기기억은 별도 SQLite 파일로 분리 관리된다."""

    def test_default_paths_are_distinct_files(self):
        from src.storage import CHECKPOINTS_DB, MEMORY_DB
        assert Path(CHECKPOINTS_DB).name != Path(MEMORY_DB).name

    def test_checkpoint_db_has_no_long_term_memory_tables(self, tmp_path):
        import sqlite3
        from src.storage import CheckpointStore

        db = tmp_path / "checkpoints.sqlite"
        CheckpointStore(db_path=db).save("s1", "step1", {"x": 1})
        with sqlite3.connect(db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert "checkpoints" in tables
        for long_term in (
            "verification_history", "sql_versions",
            "performance_baselines", "business_terms", "feedback",
        ):
            assert long_term not in tables

    def test_memory_db_holds_all_five_long_term_tables(self, tmp_path):
        import sqlite3
        from src.storage import MemoryStore

        db = tmp_path / "memory.sqlite"
        MemoryStore(db_path=db)
        with sqlite3.connect(db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert {
            "verification_history", "sql_versions",
            "performance_baselines", "business_terms", "feedback",
        } <= tables
        assert "checkpoints" not in tables

    def test_writes_do_not_cross_contaminate(self, tmp_path):
        """세션 체크포인트 저장이 장기기억 파일을 건드리지 않는다."""
        from src.storage import CheckpointStore, MemoryStore

        ck_db = tmp_path / "checkpoints.sqlite"
        mem_db = tmp_path / "memory.sqlite"
        memory = MemoryStore(db_path=mem_db)
        memory.save_business_term("기준선", "성능 비교의 출발점")

        CheckpointStore(db_path=ck_db).save("s1", "candidates", {"candidates": []})

        # 장기기억은 그대로 남아 있고, 체크포인트 파일과 물리적으로 분리되어 있다
        assert MemoryStore(db_path=mem_db).get_business_term("기준선") is not None
        assert ck_db.exists() and mem_db.exists()
        assert ck_db.resolve() != mem_db.resolve()
