"""Carrier ceilings: the setting, the generated dialplan, and every jump
in the dialplan landing on a label of its own extension."""
import re

import pytest


def jumps_bad(text):
    """(context, exten, label) for every Goto/GotoIf target that isn't a label
    of the same extension, and labels defined twice."""
    bad, ctx, cur, exts = [], None, None, {}
    for line in text.splitlines():
        if line.startswith("["):
            ctx = line.strip("[]")
            continue
        m = re.match(r"exten => ([^,]+),", line)
        if m:
            cur = (ctx, m.group(1))
            exts[cur] = []
        if line.startswith(("exten", " same")) and cur:
            exts[cur].append(line)
    for key, lines in exts.items():
        body = "\n".join(lines)
        labels = set(re.findall(r"n\(([a-z0-9]+)\)", body))
        for l in lines:
            for t in re.findall(r"\?([a-z0-9]+)\)", l) + re.findall(r"[ ,]Goto\(([a-z0-9]+)\)", l):
                if t not in labels:
                    bad.append((*key, t))
        bad += [(*key, f"twice: {x}") for x in labels if body.count(f"n({x})") > 1]
    return bad


@pytest.fixture
def setup(client):
    import pbx
    conn = pbx.get_db()
    conn.execute("INSERT INTO extensions (ext, name, password, enabled, created_at, updated_at, vm_pin, tenant_id) "
                 "VALUES ('201', 'A', 'pw', 1, 'n', 'n', '1', 1)")
    conn.execute("INSERT INTO inbound_routes (did, target, tenant_id) VALUES ('35315550000', '201', 1)")
    conn.commit()
    assert client.post("/api/pbx/tenants", json={"name": "Acme", "outbound_cid": "015559999"}).status_code == 200
    s = client.get("/api/pbx/config").json()["settings"]

    def put(**changes):
        return client.put("/api/pbx/settings", json={**s, "routes": {**s["routes"], "mobile": "default"}, **changes})
    return pbx, conn, s, put


def test_no_ceilings_by_default(setup):
    pbx, conn, s, put = setup
    assert s["trunk_max"] == {"irishvoip": "", "voxbeam": ""}


@pytest.mark.parametrize("bad", [{"irishvoip": "0"}, {"irishvoip": "201"}, {"irishvoip": "two"}, {"nope": "3"}])
def test_bad_values_refused(setup, bad):
    pbx, conn, s, put = setup
    assert put(trunk_max=bad).status_code == 400


def test_ceiling_in_the_dialplan(setup):
    pbx, conn, s, put = setup
    assert put(trunk_max={"irishvoip": "2", "voxbeam": ""}).status_code == 200
    assert put(trunk_max=None).status_code == 200 and pbx.get_settings(conn)["trunk_max_irishvoip"] == "2"
    gen = open(pbx.PORTAL_DIALPLAN).read()
    assert "[portal-trunkcount]\nexten => s,1,Set(GROUP(trunkout)=${ARG1})\n same => n,Return()" in gen
    iv = [l for l in gen.splitlines() if "@irishvoip," in l and "Dial(" in l]
    vb = [l for l in gen.splitlines() if "@voxbeam," in l and "Dial(" in l]
    assert iv and all("b(portal-trunkcount^s^1(irishvoip))" in l for l in iv)
    assert vb and not any("trunkcount" in l for l in vb)
    emergency = sum(1 for l in iv if re.search(r"PJSIP/(112|999)@", l))
    checks = gen.count("GROUP_COUNT(irishvoip@trunkout)} + ${GROUP_COUNT(irishvoip@trunkin)} >= 2]")
    assert emergency == 4 and checks == len(iv) - emergency  # 112/999 counted, never checked
    inbound = gen.split("[portal-inbound]")[1]
    branches = re.findall(r"^exten => ([^,]+),1", inbound, re.M)
    counted = re.findall(r"^exten => ([^,]+),1.*\n same => n,Set\(GROUP\(trunkin\)=\$\{CHANNEL\(endpoint\)\}\)",
                         inbound, re.M)
    assert branches and counted == branches
    assert '"${DIALSTATUS}" = "CHANUNAVAIL" | "${DIALSTATUS}" = "CONGESTION"]?busy)' in gen
    assert not jumps_bad(gen)


def test_cleared_ceilings_leave_nothing(setup):
    pbx, conn, s, put = setup
    put(trunk_max={"irishvoip": "2", "voxbeam": "5"})
    assert put(trunk_max={"irishvoip": "", "voxbeam": ""}).status_code == 200
    gen = open(pbx.PORTAL_DIALPLAN).read()
    assert not re.search("trunkcount|trunkin|trunkout|FULL", gen)
    assert not jumps_bad(gen)
