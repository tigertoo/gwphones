"""Every module with routes is registered in main.py. The desk-phones branch
(deskphones.py) was split from the admin dashboard without a main.py of its
own, so merging it needs its two routers added there: this fails until then."""
import os
import re

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")


def test_every_router_registered():
    main = open(os.path.join(BACKEND, "main.py")).read()
    for f in sorted(os.listdir(BACKEND)):
        if not f.endswith(".py") or f == "main.py":
            continue
        src = open(os.path.join(BACKEND, f)).read()
        mod = f[:-3]
        for router in re.findall(r"^(\w*router) = APIRouter", src, re.M):
            assert re.search(rf"from {mod} import .*\b{router}\b", main), f"{mod}.{router} isn't registered in main.py"
