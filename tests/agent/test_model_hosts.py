"""ADR 0011 Amendment 1: Fireworks reads the designer's reply, Baseten is its failover. The trace names the
host that actually served each call, not the one tried first."""

import pytest

from anville.agent.model import FailoverModel, ModelUnavailable, OpenAICompatModel
from anville.agent.reply import read_reply
from tests.agent.test_reply import RAISED, REPLY, keep, rules
from tests.agent.test_review import Scripted

BASE = {"MUSE_SPARK_OPEN_BASE_URL": "https://model-x.api.baseten.co/v1", "MUSE_SPARK_OPEN_API_KEY": "b",
        "MUSE_SPARK_OPEN_MODEL_ID": "meta-models/muse-spark-open-1026"}


def hosted(host, *replies):
    m = Scripted(*replies, name="muse-spark-open")
    m.host = host
    return m


def test_reply_without_a_fireworks_key_stays_on_baseten():
    m = OpenAICompatModel.from_env(BASE, "reply")
    assert isinstance(m, OpenAICompatModel) and m.host == "model-x.api.baseten.co"


def test_reply_with_a_fireworks_key_tries_fireworks_then_baseten():
    m = OpenAICompatModel.from_env({**BASE, "FIREWORKS_API_KEY": "f"}, "reply")
    assert isinstance(m, FailoverModel)
    assert [h.host for h in m.models] == ["api.fireworks.ai", "model-x.api.baseten.co"]
    assert m.models[0].name == "accounts/fireworks/models/muse-spark-open"


def test_the_fireworks_model_and_url_can_be_overridden():
    m = OpenAICompatModel.from_env({**BASE, "FIREWORKS_API_KEY": "f", "FIREWORKS_MODEL_ID": "accounts/x/models/y",
                                    "FIREWORKS_BASE_URL": "https://fw.example/v1"}, "reply")
    assert (m.models[0].name, m.models[0].host) == ("accounts/x/models/y", "fw.example")


def test_failover_serves_from_the_next_host_and_reports_it():
    m = FailoverModel([hosted("api.fireworks.ai", ModelUnavailable("HTTP 503")),
                       hosted("model-x.api.baseten.co", rules(keep()))])
    reply = m.call([], [])
    assert reply.tool == "read_reply" and m.host == "model-x.api.baseten.co"


def test_failover_raises_when_every_host_fails():
    m = FailoverModel([hosted("a", ModelUnavailable("down")), hosted("b", ModelUnavailable("also down"))])
    with pytest.raises(ModelUnavailable, match="also down"):
        m.call([], [])


def test_each_call_starts_from_the_primary_host():
    fw = hosted("api.fireworks.ai", ModelUnavailable("blip"), rules(keep()))
    m = FailoverModel([fw, hosted("model-x.api.baseten.co", rules(keep()))])
    m.call([], [])
    m.call([], [])
    assert m.host == "api.fireworks.ai"


def test_the_trace_records_the_host_that_served_the_reply():
    m = FailoverModel([hosted("api.fireworks.ai", ModelUnavailable("HTTP 503")),
                       hosted("model-x.api.baseten.co", rules(keep()))])
    found, run = read_reply(m, REPLY, RAISED, emit=lambda *a, **k: None)
    assert found and [u["host"] for u in run.usage] == ["model-x.api.baseten.co"]


def test_the_trace_records_fireworks_when_it_serves():
    m = FailoverModel([hosted("api.fireworks.ai", rules(keep())), hosted("model-x.api.baseten.co")])
    _, run = read_reply(m, REPLY, RAISED, emit=lambda *a, **k: None)
    assert [u["host"] for u in run.usage] == ["api.fireworks.ai"]


def test_the_backup_host_gets_only_the_time_left(monkeypatch):
    import anville.agent.model as model_mod
    clock = iter([100.0, 130.0])  # Fireworks used 30 of the 50 s budget before failing
    monkeypatch.setattr(model_mod.time, "monotonic", lambda: next(clock))
    fw = hosted("api.fireworks.ai", ModelUnavailable("no complete reply within 30 s"))
    bt = hosted("model-x.api.baseten.co", rules(keep()))
    FailoverModel([fw, bt]).call([], [], timeout_s=50)
    assert fw.timeouts == [50] and bt.timeouts == [20]


def test_no_time_left_means_no_backup_call(monkeypatch):
    import anville.agent.model as model_mod
    clock = iter([100.0, 150.0])
    monkeypatch.setattr(model_mod.time, "monotonic", lambda: next(clock))
    bt = hosted("model-x.api.baseten.co", rules(keep()))
    with pytest.raises(ModelUnavailable, match="slow"):
        FailoverModel([hosted("api.fireworks.ai", ModelUnavailable("slow")), bt]).call([], [], timeout_s=50)
    assert bt.sent == []


def test_the_reply_turn_reports_the_host_that_read_the_reply(part_a, shop, chamfer):
    from anville.agent.job import read_reply_turn
    events = []
    m = FailoverModel([hosted("api.fireworks.ai", ModelUnavailable("HTTP 503")),
                       hosted("model-x.api.baseten.co", rules(keep("weld clearance", group=chamfer)))])
    read_reply_turn(part_a.to_dict(), shop, {}, lambda kind, msg, **f: events.append((msg, f)),
                    "That chamfer is weld clearance, keep it.", model=m)
    read = [f for msg, f in events if msg == "Reply read"]
    assert read and read[0]["host"] == "model-x.api.baseten.co" and read[0]["calls"][0]["host"] == read[0]["host"]
