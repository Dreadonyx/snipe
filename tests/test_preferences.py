from datetime import date, datetime

import pytest

from snipe.bot import apply_setting_tap
from snipe.formatter import Alert
from snipe.preferences import UserSettings, in_quiet_hours, mentions, parse_word_list, wants

TODAY = date(2026, 10, 12)


def alert(**kw):
    return Alert(**{"message": "m", "category": "hackathon", "text": "AI Hackathon for builders", **kw})


def test_parse_word_list():
    assert parse_word_list("AI, Web  Dev,, ai ,") == ["ai", "web dev"]


@pytest.mark.parametrize("text, word, hit", [
    ("GenAI and AI hackathon", "ai", True),
    ("Email marketing in the supply chain", "ai", False),
    ("Learn C++ today", "c++", True),
    ("Web Dev bootcamp", "web dev", True),
])
def test_mentions_is_whole_word(text, word, hit):
    assert mentions(text, word) is hit


def test_no_settings_wants_everything():
    assert wants(UserSettings(), alert(), TODAY)


def test_interests_and_mutes():
    assert wants(UserSettings(interests=["ai", "web3"]), alert(), TODAY)
    assert not wants(UserSettings(interests=["web3"]), alert(), TODAY)
    assert not wants(UserSettings(muted=["builders"]), alert(), TODAY)
    # mute wins over interest
    assert not wants(UserSettings(interests=["ai"], muted=["hackathon"]), alert(), TODAY)


def test_online_only_drops_known_in_person_but_keeps_unknown():
    s = UserSettings(location="online")
    assert not wants(s, alert(mode="offline", city="Pune"), TODAY)
    assert wants(s, alert(mode="online"), TODAY)
    assert wants(s, alert(mode="hybrid"), TODAY)
    assert wants(s, alert(mode=""), TODAY)  # RSS/web items have no location info


def test_near_me_keeps_my_city_and_online():
    s = UserSettings(location="near", city="Chennai")
    assert wants(s, alert(mode="offline", city="Chennai, India"), TODAY)
    assert not wants(s, alert(mode="offline", city="Nagpur"), TODAY)
    assert wants(s, alert(mode="online"), TODAY)
    assert wants(s, alert(mode="offline", city=""), TODAY)  # unknown city: don't hide it


def test_internship_pay_filters():
    paid_only = UserSettings(paid_only=True)
    assert not wants(paid_only, alert(category="internship", paid=False), TODAY)
    assert wants(paid_only, alert(category="internship", paid=None), TODAY)
    assert wants(paid_only, alert(category="hackathon", paid=False), TODAY)  # only applies to internships

    min_10k = UserSettings(min_stipend=10000)
    assert not wants(min_10k, alert(category="internship", paid=True, stipend=6000), TODAY)
    assert wants(min_10k, alert(category="internship", paid=True, stipend=15000), TODAY)
    assert wants(min_10k, alert(category="internship", paid=True, stipend=0), TODAY)  # amount hidden


def test_deadline_filter():
    s = UserSettings(min_days_left=5)
    assert not wants(s, alert(deadline_date=date(2026, 10, 14)), TODAY)
    assert wants(s, alert(deadline_date=date(2026, 10, 20)), TODAY)
    assert wants(s, alert(deadline_date=None), TODAY)


@pytest.mark.parametrize("start, end, hour, quiet", [
    (23, 7, 23, True), (23, 7, 3, True), (23, 7, 7, False), (23, 7, 12, False),
    (0, 8, 0, True), (0, 8, 8, False), (None, None, 3, False),
])
def test_quiet_hours(start, end, hour, quiet):
    assert in_quiet_hours(UserSettings(quiet_start=start, quiet_end=end), hour) is quiet


def test_settings_round_trip_ignores_unknown_keys():
    s = UserSettings(interests=["ai"], quiet_start=None, quiet_end=None)
    assert UserSettings.from_dict({**s.to_dict(), "removed_in_future": 1}) == s


def test_settings_buttons_cycle():
    now = datetime(2026, 10, 12, 9)
    s = UserSettings()
    apply_setting_tap(s, "delivery", now)
    assert s.delivery == "digest" and s.last_digest == "2026-10-12"  # 08:00 already passed: start tomorrow
    apply_setting_tap(s, "digest_hour", now)
    assert s.digest_hour == 12
    apply_setting_tap(s, "quiet", now)
    assert s.quiet == (22, 8)
    for _ in range(3):
        apply_setting_tap(s, "quiet", now)
    assert s.quiet == (23, 7)  # 22-8 → 0-8 → off → 23-7
    apply_setting_tap(s, "location", now)
    assert s.location == "online"
    s.min_stipend = 5000
    apply_setting_tap(s, "paid", now)
    apply_setting_tap(s, "paid", now)
    assert not s.paid_only and s.min_stipend == 0
    apply_setting_tap(s, "days", now)
    assert s.min_days_left == 2
