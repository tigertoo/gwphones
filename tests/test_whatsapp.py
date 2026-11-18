"""WhatsApp calls: the setup tool talks to a fake Meta, never prints the
token or Meta's SIP password, writes the endpoint matched by Meta's
addresses only; the dialplan sends the calls to inbound routes only; and
billing shows them as incoming WhatsApp calls."""
import os

import pytest

TOKEN = "EAAGtesttoken0123456789"
PASSWORD = "Mx9-sip-pass-7Qw"
PHONE_ID = "106540352242922"
EXT_CONF = os.path.join(os.path.dirname(__file__), "..", "asterisk", "extensions.conf")


class Answer:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class FakeMeta:
    def __init__(self):
        self.calling, self.posts, self.seen_tokens = {}, [], set()

    def __call__(self, method, url, headers, params, body):
        self.seen_tokens.add(headers["Authorization"])
        assert url.startswith(f"https://graph.facebook.com/v24.0/{PHONE_ID}")
        path = url.split(PHONE_ID, 1)[1]
        if method == "GET" and path == "":
            return Answer({"display_phone_number": "+353 1 555 0199", "verified_name": "GW", "id": PHONE_ID})
        if method == "POST" and path == "/settings":
            self.posts.append(body["calling"])
            c = body["calling"]
            if "sip" in c and "srtp_key_exchange_protocol" not in self.calling:
                return Answer({"error": {"message": f"set SDES first {TOKEN}", "code": 100}}, 400)
            sip = c.get("sip")
            self.calling.update({k: v for k, v in c.items() if k != "sip"})
            if sip:
                old = self.calling.get("sip", {})
                self.calling["sip"] = {**old, **sip}
            return Answer({"success": True})
        if method == "GET" and path == "/settings":
            calling = {**self.calling}
            if "sip" in calling:
                servers = [dict(s) for s in calling["sip"].get("servers", [])]
                if (params or {}).get("include_sip_credentials") == "true":
                    for s in servers:
                        s["sip_user_password"] = PASSWORD
                calling["sip"] = {**calling["sip"], "servers": servers}
            return Answer({"calling": calling})
        raise AssertionError((method, path))


@pytest.fixture
def wa(env, monkeypatch):
    import whatsapp
    meta, lines = FakeMeta(), []

    def run(*args):
        return whatsapp.main(["--phone-id", PHONE_ID, "--graph-version", "v24.0", *args],
                             token_prompt=lambda prompt: TOKEN, send=meta, out=lines.append)
    return whatsapp, meta, lines, run


def test_setup_in_order_and_nothing_secret_shown(wa, tmp_path):
    whatsapp, meta, lines, run = wa
    # SIP before SDES is refused (and Meta's message never shows the token)
    assert run("enable-sip", "--host", "pbx.example.com", "--port", "15061") == 1
    assert TOKEN not in "\n".join(lines) and "********" in lines[-1]
    assert run("prepare") == 0
    assert meta.posts[-1] == {"status": "ENABLED", "srtp_key_exchange_protocol": "SDES",
                              "audio": {"additional_codecs": ["PCMA"]}}
    assert run("enable-sip", "--host", "PBX.example.com", "--port", "15061") == 0
    assert meta.posts[-1]["sip"]["servers"] == [{"hostname": "pbx.example.com", "port": 15061}]
    out = tmp_path / "pjsip_whatsapp.conf"
    assert run("write-conf", "--out", str(out)) == 0
    assert run("check") == 0
    shown = "\n".join(lines)
    assert PASSWORD not in shown and TOKEN not in shown
    assert whatsapp.fingerprint(PASSWORD) in shown and "SDES" in shown and "pbx.example.com:15061" in shown
    assert meta.seen_tokens == {f"Bearer {TOKEN}"}
    # the file: Meta's password, readable by root and the asterisk group only
    text = out.read_text()
    assert f"password={PASSWORD}" in text and oct(out.stat().st_mode & 0o777) == "0o640"
    assert not os.path.exists(str(out) + ".tmp")
    assert run("disable-sip") == 0 and meta.posts[-1] == {"sip": {"status": "DISABLED"}}


def test_endpoint_matched_by_metas_addresses_only(wa):
    whatsapp = wa[0]
    text = whatsapp.build_conf("+353 1 555 0199", PASSWORD, "pbx.example.com", whatsapp.META_RANGES)
    assert "match_header" not in text and "X-FB" not in text
    assert text.count("match=") == len(whatsapp.META_RANGES)
    for line in ("context=from-whatsapp", "media_encryption=sdes", "rewrite_contact=no", "direct_media=no",
                 "transport=transport-tls", "from_user=35315550199", "from_domain=pbx.example.com",
                 "username=35315550199", "outbound_auth=whatsapp-auth", "contact=sip:wa.meta.vc;transport=tls"):
        assert line in text.splitlines(), line
    # inbound calls aren't challenged: Meta answers a 407, Asterisk asks with 401
    assert not any(ln.startswith("auth=") for ln in text.splitlines())
    assert "allow=alaw" in whatsapp.build_conf("35315550199", PASSWORD, "pbx.example.com", ["157.240.0.0/16"],
                                               codecs="alaw")


@pytest.mark.parametrize("kwargs", [{"password": "x\n[evil]"}, {"password": ""}, {"host": "pbx.example.com\nx=1"},
                                    {"transport": "a b"}, {"number": "12"}, {"codecs": "g729"}])
def test_nothing_unexpected_reaches_the_file(wa, kwargs):
    whatsapp = wa[0]
    args = {"number": "35315550199", "password": PASSWORD, "host": "pbx.example.com", "ranges": ["157.240.0.0/16"],
            **kwargs}
    with pytest.raises(whatsapp.GraphError):
        whatsapp.build_conf(**args)


def test_ranges_file(wa, tmp_path):
    whatsapp = wa[0]
    f = tmp_path / "r.txt"
    f.write_text("# Meta\n157.240.0.0/16\n\n2a03:2880::/32  # v6\n")
    assert whatsapp.read_ranges(str(f)) == ["157.240.0.0/16", "2a03:2880::/32"]
    f.write_text("157.240.0.0/16\nallow all\n")
    with pytest.raises(whatsapp.GraphError):
        whatsapp.read_ranges(str(f))


def test_bad_arguments_never_ask_for_the_token(wa):
    whatsapp, meta, lines, run = wa
    asked = []
    for args in (["--phone-id", "+353 1 555 0199", "--graph-version", "v24.0", "check"],
                 ["--phone-id", PHONE_ID, "--graph-version", "24", "check"],
                 ["--phone-id", PHONE_ID, "--graph-version", "v24.0", "enable-sip", "--host", "x", "--port", "1"]):
        assert whatsapp.main(args, token_prompt=lambda p: asked.append(p) or TOKEN, send=meta, out=lines.append) != 0
    assert asked == [] and meta.posts == []


def test_write_conf_needs_sip_first(wa, tmp_path):
    whatsapp, meta, lines, run = wa
    assert run("write-conf", "--out", str(tmp_path / "w.conf")) == 1 and "enable-sip" in lines[-1]
    assert not (tmp_path / "w.conf").exists()


def test_dialplan_reaches_inbound_routes_only():
    text = open(EXT_CONF).read()
    ctx = text.split("[from-whatsapp]", 1)[1].split("\n[", 1)[0]
    assert "Goto(from-trunk," in ctx and "CALLERID(name)=WhatsApp" in ctx
    for bad in ("Dial(", "sub-outbound", "from-internal", "portal-internal", "include"):
        assert bad not in ctx, bad


def test_billing_shows_an_incoming_whatsapp_call(env):
    import billing
    row = {k: "" for k in billing.CDR_FIELDS}
    row.update({"uniqueid": "1", "channel": "PJSIP/whatsapp-0000001a", "dstchannel": "PJSIP/201-0000001b",
                "dst": "+35315550199", "start": "2026-11-18 10:00:00", "billsec": "30", "duration": "35",
                "disposition": "ANSWERED", "clid": '"WhatsApp" <+353871234567>'})
    c = billing.classify(row)
    assert c["direction"] == "in" and c["trunk"] == "whatsapp" and c["ext"] == "201" and c["status"] == "free"
    assert billing.route_label("whatsapp", "") == "WhatsApp"
    # never a choice for calls out
    import pbx
    assert "whatsapp" not in pbx.TRUNKS and "whatsapp" not in pbx.ROUTE_CHOICES
