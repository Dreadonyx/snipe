import json
from types import SimpleNamespace

import pytest

from snipe.formatter import Formatter, FormatterError, render_batch
from snipe.scanner import ScanResult


class FakeGroq:
    """Stands in for groq.Groq; returns canned replies and records prompts."""

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.prompts = reply, error, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        if self.error:
            raise self.error
        content = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.fixture
def formatter(make_config):
    def _make(reply=None, error=None):
        groq = FakeGroq(reply, error)
        return Formatter(make_config(""), client=groq), groq
    return _make


def item(**kw):
    defaults = {"title": "AI Hackathon", "url": "https://example.com/hack", "snippet": "Build things", "source": "Blog"}
    return ScanResult(**{**defaults, **kw})


OPPORTUNITY = {
    "is_opportunity": True, "name": "AI Hack <2026>", "type": "hackathon",
    "prize_or_stipend": "$5,000", "deadline": "Nov 1", "one_line": "Build & ship AI apps",
}


def test_trusted_items_skip_the_llm(formatter):
    fmt, groq = formatter()
    alert = fmt.format_alert(item(trusted=True, category="ctf", deadline="22 Oct 2026", prize="₹6,500 in prizes"))
    assert groq.prompts == []
    assert alert.category == "ctf"
    assert "⏰ <b>Deadline:</b> 22 Oct 2026" in alert.message
    assert "₹6,500 in prizes" in alert.message


def test_llm_alert_is_html_escaped(formatter):
    fmt, _ = formatter(OPPORTUNITY)
    alert = fmt.format_alert(item(url='https://example.com/a?x=1&y="2"'))
    assert alert.category == "hackathon"
    assert "<b>AI Hack &lt;2026&gt;</b>" in alert.message
    assert "Build &amp; ship AI apps" in alert.message
    assert 'href="https://example.com/a?x=1&amp;y=&quot;2&quot;"' in alert.message
    assert "💰 <b>Prize/Stipend:</b> $5,000" in alert.message


def test_link_always_comes_from_the_source_not_the_llm(formatter):
    fmt, _ = formatter({**OPPORTUNITY, "apply_url": "https://evil.example/phish"})
    assert "evil.example" not in fmt.format_alert(item()).message


@pytest.mark.parametrize("reply", [{"is_opportunity": False}, "I think this is news.", "{not json", {**OPPORTUNITY, "type": "news"}])
def test_rejections_return_none(formatter, reply):
    fmt, _ = formatter(reply)
    assert fmt.format_alert(item()) is None


def test_api_errors_raise_so_the_item_is_retried(formatter):
    fmt, _ = formatter(error=RuntimeError("rate limited"))
    with pytest.raises(FormatterError):
        fmt.format_alert(item())


def test_linkedin_event_details_reach_prompt_and_message(formatter):
    fmt, groq = formatter({**OPPORTUNITY, "type": "event", "prize_or_stipend": None, "deadline": "Not mentioned"})
    event = item(source="LinkedIn", category="event", when="Sat, 24 Oct 2026", organizer="GDG Chennai", location="Online")
    alert = fmt.format_alert(event)
    assert "Organizer: GDG Chennai" in groq.prompts[0] and "When: Sat, 24 Oct 2026" in groq.prompts[0]
    assert alert.category == "event"
    assert "📆 <b>When:</b> Sat, 24 Oct 2026" in alert.message
    assert "Register here" in alert.message
    assert "Deadline" not in alert.message and "Prize" not in alert.message


def test_scholarship_maps_to_grant_and_unknown_type_falls_back_to_source(formatter):
    fmt, _ = formatter({**OPPORTUNITY, "type": "scholarship"})
    assert fmt.format_alert(item()).category == "grant"
    fmt, _ = formatter({**OPPORTUNITY, "type": "webinar"})
    assert fmt.format_alert(item(category="event")).category == "event"


def test_alert_carries_compact_form_and_filter_facts(formatter):
    fmt, _ = formatter()
    alert = fmt.format_alert(item(trusted=True, category="internship", title="Python <Intern>", deadline="22 Oct 2026",
                                  prize="₹12,000–15,000/month", location="Online", mode="online", paid=True, stipend=15000))
    assert alert.compact == (
        '💼 <b><a href="https://example.com/hack">Python &lt;Intern&gt;</a></b>\n'
        "⏰ 22 Oct 2026 · 💰 ₹12,000–15,000/month · 📍 Online"
    )
    assert (alert.mode, alert.paid, alert.stipend) == ("online", True, 15000)
    assert "Python <Intern>" in alert.text and "Build things" in alert.text


def test_render_batch_splits_under_telegram_limit():
    entries = [f"entry {n} " + "x" * 300 for n in range(40)]
    messages = render_batch("HEADER", entries)
    assert len(messages) > 1 and all(len(m) <= 4000 for m in messages)
    assert messages[0].startswith("HEADER")
    assert sum(m.count("entry ") for m in messages) == 40
