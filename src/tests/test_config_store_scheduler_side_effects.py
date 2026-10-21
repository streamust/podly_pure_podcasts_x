from __future__ import annotations

from app import config_store


def test_apply_app_scheduler_side_effects_reconciles_both_jobs(monkeypatch) -> None:
    refresh_calls: list[tuple[int | None, int | None]] = []
    cleanup_calls: list[tuple[int | None, int | None]] = []
    monkeypatch.setattr(
        config_store,
        "_maybe_reschedule_refresh_job",
        lambda old, new: refresh_calls.append((old, new)),
    )
    monkeypatch.setattr(
        config_store,
        "_maybe_disable_cleanup_job",
        lambda old, new: cleanup_calls.append((old, new)),
    )

    config_store.apply_app_scheduler_side_effects(
        {
            "background_update_interval_minute": 30,
            "post_cleanup_retention_days": 5,
        },
        {
            "background_update_interval_minute": 10,
            "post_cleanup_retention_days": 0,
        },
    )

    assert refresh_calls == [(30, 10)]
    assert cleanup_calls == [(5, 0)]
