import json

from tw_watchdesk.app import _display_text, _format_agent_reviews, _format_db_time, _format_json_text, _proposal_status_label


def test_review_status_labels_hide_internal_review_only_code() -> None:
    assert _proposal_status_label("review_only") == "已討論，不需改版"
    assert _proposal_status_label("version_reused_locked") == "沿用既有新版但目前鎖定"


def test_review_json_format_hides_internal_review_only_codes() -> None:
    text = _format_json_text(
        json.dumps(
            {
                "proposal_status": "review_only",
                "action": "record_review_only",
                "nested": ["version_reused_applied", "insufficient_evidence"],
            }
        )
    )

    assert "review_only" not in text
    assert "record_review_only" not in text
    assert "version_reused_applied" not in text
    assert "已討論，不需改版" in text
    assert "沿用既有新版並套用" in text
    assert "證據不足" in text


def test_display_text_hides_internal_review_status_codes() -> None:
    text = _display_text("2026-07-06 當沖 多 Agent 盤後檢討：review_only")

    assert "review_only" not in text
    assert text == "2026-07-06 當沖 多 Agent 盤後檢討：已討論，不需改版"


def test_db_time_formats_utc_iso_as_local_detail_time() -> None:
    assert _format_db_time("2026-07-03T01:10:03+00:00", "Asia/Taipei") == "2026-07-03 09:10:03"


def test_agent_review_format_hides_internal_action_codes() -> None:
    text = _format_agent_reviews(
        [
            {
                "created_at": "2026-07-06T07:00:00+00:00",
                "agent_name": "SwingAgent",
                "status": "ok",
                "action": "record_review_only",
                "confidence": 0.8,
                "evidence_quality": "sufficient",
                "output_hash": "hash",
                "output_json": json.dumps({"action": "record_review_only"}),
            }
        ],
        "Asia/Taipei",
    )

    assert "record_review_only" not in text
    assert "已討論，不需改版" in text
    assert "成功" in text
