"""Tests for test_origin_classifier.py — canonical consolidation of every
test/synthetic-origin marker this campaign independently verified
(2026-09-08 self-stewardship finding)."""
from __future__ import annotations

import test_origin_classifier as toc


def test_real_device_client_not_flagged():
    assert toc.is_test_or_synthetic_origin(requested_by="device_client") is False


def test_real_mrsilent_app_not_flagged():
    assert toc.is_test_or_synthetic_origin(requested_by="mrsilent_app") is False


def test_word_boundary_does_not_false_positive_on_latest_or_contest():
    assert toc.is_test_or_synthetic_origin(text_blob="latest_uploader processed the contest winner") is False


def test_exact_requested_by_test_flagged():
    assert toc.is_test_or_synthetic_origin(requested_by="test") is True


def test_exact_requested_by_acceptance_test_flagged():
    assert toc.is_test_or_synthetic_origin(requested_by="acceptance_test") is True


def test_capability_needed_test_flagged():
    assert toc.is_test_or_synthetic_origin(capability_needed="test") is True


def test_media_failure_taxonomy_flagged_the_original_live_leak():
    """The exact real live event this campaign found: observation
    evidence.requested_by == 'test_media_failure_taxonomy' incorrectly
    escalated as a production repeated-retry defect."""
    assert toc.is_test_or_synthetic_origin(requested_by="test_media_failure_taxonomy") is True


def test_fake_canary_job_flagged_the_second_live_leak():
    """The exact real live escalation this campaign found: job_id
    'fake-canary-job' slipped past the original job_id.startswith('test-')
    check."""
    assert toc.is_test_or_synthetic_origin(job_id="fake-canary-job") is True


def test_fake_marker_in_free_text_flagged():
    assert toc.is_test_or_synthetic_origin(text_blob="promote job fake-canary-job to a real path") is True


def test_synthetic_marker_flagged_the_campaign_34c0fbe0_leak():
    """The Telegram-channel leak this campaign found and fixed: campaign
    34c0fbe0's own objective text said 'synthetic end-to-end test ...'."""
    assert toc.is_test_or_synthetic_origin(text_blob="synthetic end-to-end test 45c7b03c: create file A") is True


def test_smoke_test_and_live_sensor_test_flagged():
    assert toc.is_test_or_synthetic_origin(requested_by="smoke_test") is True
    assert toc.is_test_or_synthetic_origin(requested_by="live_sensor:test") is True


def test_no_fields_provided_is_never_flagged():
    assert toc.is_test_or_synthetic_origin() is False
