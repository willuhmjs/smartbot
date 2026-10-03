import asyncio
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DISCORD_TOKEN", "test")

from store import Store  # noqa: E402
from moderation import Moderator, SpamTracker, Strikes, Verdict, escalation_step, parse_verdict  # noqa: E402


def test_parse_verdict():
    v = parse_verdict('<think>hmm</think>{"violation": true, "rule": "2. No spam", "severity": "High", "reason": "ad"}')
    assert v == Verdict(True, "2. No spam", "high", "ad")
    assert not parse_verdict('{"violation": true, "severity": "none"}').violation
    assert parse_verdict("no json here") is None


def test_strikes_expire_and_escalate(tmp_path):
    db = str(tmp_path / "smartbot.db")
    s = Strikes(Store(db))
    s.store.add_strike(1, 5, 9, "old", at=time.time() - 31 * 86400)   # expired
    assert s.add(1, 5, 2, "a", 30) == (0, 2)
    assert s.add(1, 5, 2, "b", 30, by=7) == (2, 4)
    assert Strikes(Store(db)).total(1, 5, 30) == 4                      # persisted
    assert s.clear(1, 5) == 3 and s.total(1, 5, 30) == 0
    assert s.store.db.execute("SELECT COUNT(*) FROM strikes").fetchone()[0] == 3  # history kept
    steps = [{"strikes": 3, "action": "timeout", "minutes": 60}, {"strikes": 5, "action": "ban"}]
    assert escalation_step(steps, 2, 4)["action"] == "timeout"
    assert escalation_step(steps, 1, 6)["action"] == "ban"       # jumping past both: the harsher one
    assert escalation_step(steps, 3, 4) is None                  # already past 3


def msg(content, uid=5, mentions=()):
    return SimpleNamespace(content=content, guild=SimpleNamespace(id=1), author=SimpleNamespace(id=uid),
                           raw_mentions=list(mentions), raw_role_mentions=[])


def test_spam_tracker():
    t = SpamTracker()
    assert t.check(msg("hi"), 3, 5, 3, 4, now=0) is None
    assert t.check(msg("yo"), 3, 5, 3, 4, now=1) is None
    assert t.check(msg("hey"), 3, 5, 3, 4, now=2) is None
    reason, offending = t.check(msg("sup"), 3, 5, 3, 4, now=3)
    assert reason.startswith("flooding") and len(offending) == 4
    t = SpamTracker()
    for i in range(2):
        assert t.check(msg("BUY NOW"), 10, 5, 3, 4, now=i * 10) is None
    assert t.check(msg("buy now "), 10, 5, 3, 4, now=25)[0].startswith("repeating")
    assert t.check(msg("@all", mentions=range(5)), 10, 5, 3, 4, now=30)[0].startswith("mass mentions")


class FakeMessage:
    def __init__(self):
        self.deleted = False
        self.content = "you are all idiots"
        self.guild = SimpleNamespace(id=1, name="g")
        self.author = SimpleNamespace(id=5, mention="<@5>", display_name="bob", timeouts=[])
        self.channel = SimpleNamespace(id=2, mention="#c", sent=[])

        async def send(text, **_):
            self.channel.sent.append(text)
        self.channel.send = send

        async def timeout(duration, reason=None):
            self.author.timeouts.append(duration)
        self.author.timeout = timeout

    async def delete(self):
        self.deleted = True


def test_automod_tiers_strikes_and_escalation(tmp_path):
    settings = {"automod_tiers": {"low": {"action": "log", "strikes": 0}, "medium": {"action": "delete_warn", "strikes": 2},
                                  "high": {"action": "delete_warn", "strikes": 3}},
                "strike_expiry_days": 30, "escalation": [{"strikes": 3, "action": "timeout", "minutes": 10}]}
    bot = SimpleNamespace(cfg=SimpleNamespace(owner_ids=frozenset()), store=Store(":memory:"),
                          profiles=SimpleNamespace(get=lambda gid, key: settings[key]))
    mod = Moderator(bot)
    logged = []

    async def log(guild, text):
        logged.append(text)
    mod.log = log
    verdicts = iter([Verdict(True, "Be respectful", "low", "rude"), Verdict(True, "Be respectful", "medium", "insult"),
                     Verdict(True, "Be respectful", "medium", "insult")])

    async def classify(*a, **k):
        return next(verdicts)
    mod.classify = classify

    m = FakeMessage()
    assert asyncio.run(mod._automod(m)) is False and not m.deleted        # low: log only
    assert asyncio.run(mod._automod(m)) is True and m.deleted              # medium: delete + warn, 2 strikes
    assert "Be respectful" in m.channel.sent[0] and not m.author.timeouts
    asyncio.run(mod._automod(m))                                           # 4 strikes: crosses 3 -> timeout
    assert len(m.author.timeouts) == 1 and m.author.timeouts[0].total_seconds() == 600
    assert any("timed out" in line for line in logged)
