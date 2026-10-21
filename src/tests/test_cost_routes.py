import json
import subprocess
from datetime import datetime
from typing import Any

import pytest

from app.extensions import db
from app.model_call_token_backfill import backfill_model_call_token_usage
from app.models import (
    Feed,
    Identification,
    ModelCall,
    Post,
    ProcessingJob,
    TranscriptSegment,
    User,
    UserFeed,
)
from app.routes import cost_routes
from app.routes.cost_routes import _model_call_cost, costs_bp


def test_web_cost_rates_batch_helper_and_preserve_rust_contract(app, monkeypatch):
    from app import pricing_client

    monkeypatch.setattr(pricing_client, "_cache", pricing_client.OrderedDict())
    requests = []

    def lookup(command, **kwargs):
        requests.append(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps([[0.000001, 0.0, 0.000002]])
        )

    monkeypatch.setattr(pricing_client.subprocess, "run", lookup)
    with app.app_context():
        feed = Feed(title="Rate Feed", rss_url="https://example.com/rates.xml")
        db.session.add(feed)
        db.session.flush()
        post = Post(
            feed_id=feed.id,
            guid="rate-post",
            title="Rates",
            download_url="https://example.com/audio.mp3",
        )
        db.session.add(post)
        db.session.flush()
        call = ModelCall(
            post_id=post.id,
            model_name="test-model",
            service_tier="flex",
            first_segment_sequence_num=0,
            last_segment_sequence_num=1,
            prompt="classify",
            status="success",
            prompt_tokens=100,
            cached_prompt_tokens=50,
            completion_tokens=10,
        )
        db.session.add(call)
        db.session.commit()
        app.config["PODLY_APP_ROLE"] = "web"
        payload = cost_routes._build_rust_rates_payload({})
        assert payload["rates"] == {
            "test-model|flex": {
                "input": 0.000001,
                "cached_input": 0.000001,
                "output": 0.000002,
            }
        }
        assert round(_model_call_cost(call), 8) == 0.00012
    assert requests == [[["test-model", "flex"]]]


def test_admin_costs_uses_litellm_tokens_and_configured_audio_rates(
    app: Any, monkeypatch: Any
) -> None:
    app.register_blueprint(costs_bp)
    monkeypatch.setattr(cost_routes, "require_admin", lambda _action: (None, None))
    monkeypatch.setattr(
        cost_routes,
        "read_combined",
        lambda: {
            "app": {
                "cost_rate_per_hour": 0.04,
                "whisper_cost_rate_per_hour": 0.04,
                "ina_cost_rate_per_hour": 0.02,
            }
        },
    )

    with app.app_context():
        feed = Feed(title="Cost Feed", rss_url="https://example.com/feed.xml")
        user_a = User(username="a", password_hash="x", role="user")
        user_b = User(username="b", password_hash="x", role="user")
        db.session.add_all([feed, user_a, user_b])
        db.session.flush()
        db.session.add_all(
            [
                UserFeed(feed_id=feed.id, user_id=user_a.id),
                UserFeed(feed_id=feed.id, user_id=user_b.id),
            ]
        )
        post = Post(
            feed_id=feed.id,
            guid="cost-guid",
            download_url="https://example.com/audio.mp3",
            title="Cost Episode",
            duration=3600,
        )
        db.session.add(post)
        db.session.flush()
        db.session.add(
            ProcessingJob(
                post_guid=post.guid,
                status="completed",
                completed_at=datetime(2026, 5, 15, 12, 0, 0),
            )
        )
        db.session.add_all(
            [
                ModelCall(
                    post_id=post.id,
                    first_segment_sequence_num=0,
                    last_segment_sequence_num=1,
                    model_name="gpt-4o-mini",
                    prompt="classify",
                    status="success",
                    prompt_tokens=1_000_000,
                    cached_prompt_tokens=500_000,
                    completion_tokens=250_000,
                    total_tokens=1_250_000,
                ),
                ModelCall(
                    post_id=post.id,
                    first_segment_sequence_num=0,
                    last_segment_sequence_num=-1,
                    model_name="whisper-large-v3-turbo",
                    prompt="Whisper transcription job",
                    status="success",
                ),
                ModelCall(
                    post_id=post.id,
                    first_segment_sequence_num=0,
                    last_segment_sequence_num=-1,
                    model_name="ina:speech_music_noise",
                    prompt="INA speech/music/noise",
                    status="success",
                ),
            ]
        )
        db.session.commit()

        response = app.test_client().get("/api/admin/costs?year=2026&month=5")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total_llm_cost"] == 0.2625
    assert payload["total_whisper_cost"] == 0.04
    assert payload["total_ina_cost"] == 0.02
    assert payload["total_cost"] == 0.3225
    assert payload["feeds"][0]["llm_cost"] == 0.2625
    assert payload["feeds"][0]["whisper_cost"] == 0.04
    assert payload["feeds"][0]["ina_cost"] == 0.02
    assert payload["users"][0]["monthly_cost"] == 0.1612
    assert payload["users"][1]["monthly_cost"] == 0.1612


def test_admin_costs_skips_whisper_and_ina_when_no_matching_model_calls(
    app: Any, monkeypatch: Any
) -> None:
    app.register_blueprint(costs_bp)
    monkeypatch.setattr(cost_routes, "require_admin", lambda _action: (None, None))
    monkeypatch.setattr(
        cost_routes,
        "read_combined",
        lambda: {
            "app": {
                "cost_rate_per_hour": 0.04,
                "whisper_cost_rate_per_hour": 0.04,
                "ina_cost_rate_per_hour": 0.02,
            }
        },
    )

    with app.app_context():
        feed = Feed(title="No Audio Feed", rss_url="https://example.com/feed.xml")
        user = User(username="solo", password_hash="x", role="user")
        db.session.add_all([feed, user])
        db.session.flush()
        db.session.add(UserFeed(feed_id=feed.id, user_id=user.id))
        post = Post(
            feed_id=feed.id,
            guid="no-audio-guid",
            download_url="https://example.com/audio.mp3",
            title="No Audio Episode",
            duration=3600,
        )
        db.session.add(post)
        db.session.flush()
        db.session.add(
            ProcessingJob(
                post_guid=post.guid,
                status="completed",
                completed_at=datetime(2026, 5, 15, 12, 0, 0),
            )
        )
        # Only an LLM call — no Whisper, no INA.
        db.session.add(
            ModelCall(
                post_id=post.id,
                first_segment_sequence_num=0,
                last_segment_sequence_num=1,
                model_name="gpt-4o-mini",
                prompt="classify",
                status="success",
                prompt_tokens=1_000_000,
                cached_prompt_tokens=500_000,
                completion_tokens=250_000,
                total_tokens=1_750_000,
            )
        )
        db.session.commit()

        response = app.test_client().get("/api/admin/costs?year=2026&month=5")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total_whisper_cost"] == 0.0
    assert payload["total_ina_cost"] == 0.0
    assert payload["feeds"][0]["whisper_cost"] == 0.0
    assert payload["feeds"][0]["ina_cost"] == 0.0


def test_admin_costs_uses_rust_sidecar_response_when_available(
    app: Any, monkeypatch: Any
) -> None:
    """When the Rust path returns a payload, the route forwards it and enriches
    Stripe subscription amounts (sidecar can't reach Stripe so it leaves
    ``subscription_amount_cents`` null)."""
    app.register_blueprint(costs_bp)
    monkeypatch.setenv("PODLY_STRIPE_BILLING_ENABLED", "true")
    monkeypatch.setattr(cost_routes, "require_admin", lambda _action: (None, None))
    monkeypatch.setattr(cost_routes, "read_combined", lambda: {"app": {}})
    monkeypatch.setattr(
        cost_routes,
        "try_render_admin_costs",
        lambda **_kwargs: {
            "year": 2026,
            "month": 5,
            "total_cost": 0.5,
            "cost_rate_per_hour": 0.04,
            "whisper_cost_rate_per_hour": 0.04,
            "ina_cost_rate_per_hour": 0.02,
            "total_llm_cost": 0.3,
            "total_whisper_cost": 0.1,
            "total_ina_cost": 0.1,
            "users": [
                {
                    "id": 1,
                    "username": "a",
                    "stripe_subscription_id": None,
                    "subscription_amount_cents": None,
                    "monthly_cost": 0.25,
                },
                {
                    "id": 2,
                    "username": "b",
                    "stripe_subscription_id": "sub_x",
                    "subscription_amount_cents": None,
                    "monthly_cost": 0.25,
                },
            ],
            "feeds": [],
        },
    )
    monkeypatch.setattr(
        "app.billing_cache.fetch_subscription_amount",
        lambda _sub_id: 999,
    )

    with app.app_context():
        response = app.test_client().get("/api/admin/costs?year=2026&month=5")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total_cost"] == 0.5
    # User with Stripe id gets the cents filled in; the other stays null.
    assert payload["users"][0]["subscription_amount_cents"] is None
    assert payload["users"][1]["subscription_amount_cents"] == 999


def test_admin_costs_falls_back_to_python_when_rust_returns_none(
    app: Any, monkeypatch: Any
) -> None:
    """The Rust wrapper returns None when the flag is off or the sidecar
    fails. The route must then fall through to the Python implementation."""
    app.register_blueprint(costs_bp)
    monkeypatch.setattr(cost_routes, "require_admin", lambda _action: (None, None))
    monkeypatch.setattr(
        cost_routes,
        "read_combined",
        lambda: {
            "app": {
                "cost_rate_per_hour": 0.04,
                "whisper_cost_rate_per_hour": 0.04,
                "ina_cost_rate_per_hour": 0.02,
            }
        },
    )
    monkeypatch.setattr(cost_routes, "try_render_admin_costs", lambda **_kwargs: None)

    with app.app_context():
        feed = Feed(title="Fallback Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        response = app.test_client().get("/api/admin/costs?year=2026&month=5")

    assert response.status_code == 200
    payload = response.get_json()
    # Python path produced the envelope (no episodes yet, but the shape is
    # there, which is the cheapest way to prove the fallback ran).
    assert payload["total_cost"] == 0.0
    assert payload["feeds"][0]["title"] == "Fallback Feed"


def test_admin_costs_calls_uses_rust_sidecar_response_when_available(
    app: Any, monkeypatch: Any
) -> None:
    app.register_blueprint(costs_bp)
    monkeypatch.setattr(cost_routes, "require_admin", lambda _action: (None, None))
    monkeypatch.setattr(cost_routes, "read_combined", lambda: {"app": {}})
    monkeypatch.setattr(
        cost_routes,
        "try_render_admin_costs_calls",
        lambda **_kwargs: {
            "calls": [{"id": 7, "model_name": "gpt-4o-mini", "estimated_cost": 0.001}],
            "total": 1,
            "page": 1,
            "per_page": 50,
            "pages": 1,
        },
    )

    with app.app_context():
        response = app.test_client().get("/api/admin/costs/calls?page=1&per_page=50")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 1
    assert payload["calls"][0]["id"] == 7


def test_model_call_cost_prices_gemini_flex_at_half_base_rate(monkeypatch) -> None:
    import litellm

    # Pin the rate fixture: vendor pricing changes must not change this test
    # of the fallback discount and cached-token arithmetic.
    monkeypatch.setattr(
        litellm,
        "model_cost",
        {
            "gemini/gemini-3-flash-preview": {
                "input_cost_per_token": 0.50 / 1_000_000,
                "cache_read_input_token_cost": 0.05 / 1_000_000,
                "output_cost_per_token": 3.00 / 1_000_000,
            }
        },
    )
    call = ModelCall(
        model_name="gemini/gemini-3-flash-preview",
        service_tier="flex",
        prompt_tokens=1_000_000,
        cached_prompt_tokens=500_000,
        completion_tokens=250_000,
    )

    # With no explicit *_flex keys, apply the Flex 50% discount.
    assert _model_call_cost(call) == 0.5125


def test_model_call_cost_uses_openai_flex_rates_without_double_counting_cache(
    monkeypatch,
) -> None:
    import litellm

    monkeypatch.setattr(
        litellm,
        "model_cost",
        {
            "openai/gpt-5-mini": {
                "input_cost_per_token_flex": 0.125 / 1_000_000,
                "cache_read_input_token_cost_flex": 0.0125 / 1_000_000,
                "output_cost_per_token_flex": 1.0 / 1_000_000,
            }
        },
    )
    call = ModelCall(
        model_name="openai/gpt-5-mini",
        service_tier="flex",
        prompt_tokens=1_000_000,
        cached_prompt_tokens=500_000,
        completion_tokens=250_000,
    )

    # This fixture provides explicit Flex rates. OpenAI includes
    # cached tokens inside prompt_tokens, so only the uncached half gets the
    # regular Flex input rate.
    assert _model_call_cost(call) == 0.31875


def test_backfill_model_call_token_usage_dry_run_leaves_rows_unchanged(
    app: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        "app.model_call_token_backfill._estimate_text_tokens",
        lambda _model_name, text: len(text.split()),
    )

    with app.app_context():
        feed = Feed(title="Backfill Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.flush()
        post = Post(
            feed_id=feed.id,
            guid="backfill-dry-run",
            download_url="https://example.com/audio.mp3",
            title="Backfill Dry Run",
        )
        db.session.add(post)
        db.session.flush()
        model_call = ModelCall(
            post_id=post.id,
            first_segment_sequence_num=0,
            last_segment_sequence_num=1,
            model_name="gpt-4o-mini",
            prompt="one two three",
            response="four five",
            status="success",
        )
        db.session.add(model_call)
        db.session.commit()

        result = backfill_model_call_token_usage(apply=False)
        db.session.refresh(model_call)

    assert result["scanned"] == 1
    assert result["eligible"] == 1
    assert result["would_update"] == 1
    assert result["updated"] == 0
    assert model_call.prompt_tokens is None
    assert model_call.cached_prompt_tokens is None
    assert model_call.completion_tokens is None
    assert model_call.total_tokens is None


def test_backfill_model_call_token_usage_apply_updates_missing_tokens_only(
    app: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        "app.model_call_token_backfill._estimate_text_tokens",
        lambda _model_name, text: len(text.split()),
    )

    with app.app_context():
        feed = Feed(title="Backfill Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.flush()
        post = Post(
            feed_id=feed.id,
            guid="backfill-apply",
            download_url="https://example.com/audio.mp3",
            title="Backfill Apply",
        )
        db.session.add(post)
        db.session.flush()
        model_call = ModelCall(
            post_id=post.id,
            first_segment_sequence_num=0,
            last_segment_sequence_num=1,
            model_name="gpt-4o-mini",
            prompt="one two three",
            response="four five",
            status="success",
        )
        whisper_call = ModelCall(
            post_id=post.id,
            first_segment_sequence_num=0,
            last_segment_sequence_num=-1,
            model_name="whisper-large-v3-turbo",
            prompt="Whisper transcription job",
            response="transcript",
            status="success",
        )
        db.session.add_all([model_call, whisper_call])
        db.session.commit()

        result = backfill_model_call_token_usage(apply=True)
        db.session.refresh(model_call)
        db.session.refresh(whisper_call)

    assert result["scanned"] == 2
    assert result["eligible"] == 1
    assert result["would_update"] == 1
    assert result["updated"] == 1
    assert result["skipped_non_llm"] == 1
    assert model_call.prompt_tokens == 3
    assert model_call.cached_prompt_tokens is None
    assert model_call.completion_tokens == 2
    assert model_call.total_tokens == 5
    assert whisper_call.prompt_tokens is None


def test_backfill_token_usage_endpoint_validates_limit(
    app: Any, monkeypatch: Any
) -> None:
    app.register_blueprint(costs_bp)
    monkeypatch.setattr(cost_routes, "require_admin", lambda _action: (None, None))

    response = app.test_client().post(
        "/api/admin/costs/backfill-token-usage",
        json={"apply": False, "limit": 0},
    )

    assert response.status_code == 400


@pytest.mark.parametrize(
    ("duration", "ad_confidences"),
    [
        (3000, [0.95]),  # ads cut: processed length excludes them
        (3600, [0.2]),  # low-confidence ad was never cut
        (3000, [0.95, 0.9]),  # two model calls flagged the same segment
        (None, []),  # duration cleared after processing
    ],
)
def test_admin_costs_measure_original_length_from_transcript(
    app: Any, monkeypatch: Any, duration: int | None, ad_confidences: list[float]
) -> None:
    app.register_blueprint(costs_bp)
    monkeypatch.setattr(cost_routes, "require_admin", lambda _action: (None, None))
    monkeypatch.setattr(
        cost_routes,
        "read_combined",
        lambda: {"app": {"whisper_cost_rate_per_hour": 0.04}},
    )
    monkeypatch.setattr(cost_routes, "try_render_admin_costs", lambda **_: None)

    with app.app_context():
        feed = Feed(title="Cost Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.flush()
        post = Post(
            feed_id=feed.id,
            guid="cost-guid",
            download_url="https://example.com/audio.mp3",
            title="Cost Episode",
            duration=duration,
        )
        db.session.add(post)
        db.session.flush()
        db.session.add(
            ProcessingJob(
                post_guid=post.guid,
                status="completed",
                completed_at=datetime(2026, 5, 15, 12, 0, 0),
            )
        )
        ad = TranscriptSegment(
            post_id=post.id, sequence_num=0, start_time=0, end_time=600, text="Ad"
        )
        content = TranscriptSegment(
            post_id=post.id, sequence_num=1, start_time=600, end_time=3600, text="Show"
        )
        whisper = ModelCall(
            post_id=post.id,
            first_segment_sequence_num=0,
            last_segment_sequence_num=-1,
            model_name="whisper-large-v3-turbo",
            prompt="Whisper transcription job",
            status="success",
        )
        db.session.add_all([ad, content, whisper])
        db.session.flush()
        for index, confidence in enumerate(ad_confidences):
            call = ModelCall(
                post_id=post.id,
                first_segment_sequence_num=0,
                last_segment_sequence_num=1,
                model_name=f"classifier-{index}",
                prompt="classify",
                status="success",
            )
            db.session.add(call)
            db.session.flush()
            db.session.add(
                Identification(
                    transcript_segment_id=ad.id,
                    model_call_id=call.id,
                    label="ad",
                    confidence=confidence,
                )
            )
        db.session.commit()

        response = app.test_client().get("/api/admin/costs?year=2026&month=5")

    payload = response.get_json()
    assert payload["total_audio_hours"] == 1.0
    assert payload["total_whisper_cost"] == 0.04
