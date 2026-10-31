import json
import os
import re
import unittest

from _load import ROOT
from krcommon import krx

SKILLS = ["trader-kr-scanner", "trader-kr-entry", "trader-kr-exit", "trader-kr-timemachine", "trader-kr-budget",
          "trader-kr-risk", "trader-kr-journal", "trader-kr-lab", "trader-kr-today", "trader-kr-broker"]
SCRIPTS = {"trader-kr-scanner": "scan.py", "trader-kr-entry": "entry.py", "trader-kr-exit": "exit.py",
           "trader-kr-timemachine": "timemachine.py", "trader-kr-budget": "size.py", "trader-kr-risk": "check.py",
           "trader-kr-journal": "journal.py", "trader-kr-lab": "lab.py", "trader-kr-today": "today.py",
           "trader-kr-broker": "broker.py"}


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def text_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "trader-kr")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".txt")) or f in ("LICENSE", ".gitignore"):
                yield os.path.join(base, f)


def code_files():
    for d in SKILLS + ["krcommon"]:
        for f in os.listdir(os.path.join(ROOT, d)):
            if f.endswith(".py"):
                yield os.path.join(ROOT, d, f)


class Repo(unittest.TestCase):
    def test_skill_folders(self):
        found = sorted(d for d in os.listdir(ROOT) if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_frontmatter_and_scripts(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, s)
            self.assertGreater(len(text.splitlines()), 60, s)
            self.assertIn('${CLAUDE_SKILL_DIR}/%s' % SCRIPTS[s], text, s)
            self.assertTrue(os.path.isfile(os.path.join(ROOT, s, SCRIPTS[s])), s)

    def test_every_skill_carries_the_safety_rule(self):
        for s in SKILLS:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)
            self.assertIn("채팅에 붙여 넣지 않습니다", text, s)

    def test_no_toss_orders_and_no_live_trading(self):
        live_host = "openapi." + "koreainvestment.com"           # 실전 서버. 모의는 openapivts
        for p in code_files():
            with open(p, encoding="utf-8") as fh:
                src = fh.read()
            rel = os.path.relpath(p, ROOT)
            for bad in ("conditional-orders", "buying-power", "sellable-quantity", "/modify", "/cancel"):
                self.assertNotIn(bad, src, "%s calls %s" % (rel, bad))
            if rel.replace(os.sep, "/") != "krcommon/tossacct.py":   # 토스 계좌는 이 파일만 읽는다
                for bad in ("/api/v1/orders", "X-Tossinvest-Account", "/api/v1/accounts", "/api/v1/holdings"):
                    self.assertNotIn(bad, src, "%s calls %s" % (rel, bad))
            for bad in ("TTTC0012U", "TTTC0011U", "TTTC0013U", "TTTC0802U", "TTTC0801U", "TTTC8434R"):
                self.assertNotIn(bad, src, "%s has a live-account TR id %s" % (rel, bad))
            if rel.replace(os.sep, "/") != "krcommon/kis.py":
                self.assertNotIn("/trading/order-cash", src, "%s calls the order API directly" % rel)
        for p in text_files():
            with open(p, encoding="utf-8", errors="replace") as fh:
                self.assertNotIn(live_host, fh.read(), os.path.relpath(p, ROOT))

    def test_toss_account_code_only_reads(self):
        with open(os.path.join(ROOT, "krcommon", "tossacct.py"), encoding="utf-8") as fh:
            acct = fh.read()
        for bad in ('"POST"', "'POST'", '"PUT"', '"DELETE"', '"PATCH"', "transport(", "urllib", "urlopen"):
            self.assertNotIn(bad, acct, "tossacct.py has %s" % bad)
        self.assertEqual(re.findall(r'self\._get\("([^"]+)"', acct),
                         ["/api/v1/accounts", "/api/v1/holdings", "/api/v1/orders"])
        self.assertEqual(re.findall(r"self\.c\.(\w+)\(", acct), ["get"])
        with open(os.path.join(ROOT, "krcommon", "toss.py"), encoding="utf-8") as fh:
            client = fh.read()
        # 토스 클라이언트가 GET 아닌 요청을 보내는 곳은 토큰 발급 하나뿐이다
        self.assertEqual(re.findall(r'transport\("(\w+)"', client), ["POST", "GET"])
        self.assertIn('self.transport("POST", self.base + "/oauth2/token"', client)

    def test_config_example_is_safe_and_complete(self):
        with open(os.path.join(ROOT, "trader-kr-budget", "config.example.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertEqual(sorted(cfg), sorted(krx.CONFIG_DEFAULTS))
        for k in ("send_orders", "allow_short", "allow_margin", "allow_preferred", "allow_etf"):
            self.assertIs(cfg[k], False, k)
        self.assertIs(cfg["require_stop"], True)
        self.assertIs(cfg["require_market_status"], True)
        for k, v in cfg.items():
            if k != "budget":
                self.assertEqual(v, krx.CONFIG_DEFAULTS[k], k)

    def test_plugin_manifest(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "day-trader-skill-kr")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["license"], "MIT")
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json"), encoding="utf-8") as fh:
            market = json.load(fh)
        self.assertEqual(market["plugins"][0]["name"], "day-trader-skill-kr")

    def test_license_keeps_the_original_notice(self):
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
            lic = fh.read()
        self.assertIn("Copyright (c) 2026 Jake Schincariol", lic)
        self.assertIn("MIT License", lic)

    def test_readme_names_every_skill(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        for s in SKILLS:
            self.assertIn("/" + s, readme)
        self.assertIn("not financial advice", readme)

    def test_no_em_dashes_or_live_keys(self):
        for p in text_files():
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(p, ROOT)
            self.assertNotIn(chr(0x2014), text, rel)
            self.assertNotRegex(text, r"tsck_live_[A-Za-z0-9]{12,}", rel)

    def test_gitignore_keeps_toss_data_and_keys_out(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as fh:
            ig = fh.read()
        for line in ("trader-kr/", "credentials*.json", "token*.json"):
            self.assertIn(line, ig)

    def test_tools_run_on_python_3_8(self):
        for p in code_files():
            with open(p, encoding="utf-8") as fh:
                src = fh.read()
            rel = os.path.relpath(p, ROOT)
            self.assertNotIn("import zoneinfo", src, rel)
            self.assertNotIn(".removeprefix(", src, rel)
            self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", rel)
            for g in ("list[", "dict[", "tuple["):
                self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(g), rel)


if __name__ == "__main__":
    unittest.main()
