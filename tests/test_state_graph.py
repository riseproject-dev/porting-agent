# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The data and the page of the interactive state graph."""

import json
import re
import unittest
from pathlib import Path

import lifecycle as lc
from scripts import generate_doc
from scripts import generate_state_machine_doc as gen

ROOT = Path(__file__).resolve().parent.parent
POLICY = json.loads((ROOT / "policy.json").read_text())
DATA = json.loads((ROOT / "docs/state-machine/graph.json").read_text())
PAGE = (ROOT / "docs/state-machine/graph.md").read_text()
TABLE = (ROOT / "docs/state-machine/table.md").read_text()
SCSS = (ROOT / "docs/_sass/custom/custom.scss").read_text()
SCRIPT = (ROOT / "docs/assets/js/state-graph.js").read_text()
EDGES = DATA["edges"]
NODES = {n["id"]: n for n in DATA["nodes"]}


class TestGraphData(unittest.TestCase):
    def test_committed_files_match_the_table(self):
        rendered = gen.render(POLICY)
        for path in (gen.GRAPH_DATA, gen.OUT / "graph.md"):
            self.assertEqual((ROOT / path).read_text(), rendered[path],
                             f"{path} is stale; run python -m scripts.generate_state_machine_doc")

    def test_nodes_are_the_states_of_the_table(self):
        self.assertEqual(list(NODES), list(lc.LIFECYCLE))
        for key, state in lc.LIFECYCLE.items():
            node = NODES[key]
            self.assertEqual((node["kind"], node["requires"]), (state.kind, list(state.requires)))
            self.assertEqual(node["summary"], lc.first_sentence(state.description))
            self.assertIn(node["kind"], DATA["kinds"])

    def test_edges_have_unique_ids_and_known_ends(self):
        self.assertEqual(len({e["id"] for e in EDGES}), len(EDGES))
        for e in EDGES:
            self.assertIn(e["source"], NODES)
            self.assertIn(e["target"], NODES)
            self.assertEqual(e["self"], e["source"] == e["target"])
            self.assertIn(e["trigger"], DATA["triggers"])

    def test_edges_by_origin_match_the_table(self):
        rows = {(e["source"], e["verb"], e["target"]) for e in EDGES if e["origin"] == "table"}
        self.assertEqual(rows, {(e.source, e.verb, e.target) for e in lc.table_edges()
                                if not lc.is_rule_row(e)})
        rules = {(e["source"], e["verb"], e["target"]) for e in EDGES if e["origin"] == "rule"}
        self.assertEqual(rules, {(e.source, e.verb, e.target) for e in lc.table_edges()
                                 if lc.is_rule_row(e)})
        everything = {(s, v, t) for s, v, _, t in lc.requirement_edges()}
        self.assertEqual({(e["source"], e["verb"], e["target"]) for e in EDGES}, everything)
        self.assertEqual(len(EDGES), sum(1 for _ in lc.requirement_edges()))
        for e in EDGES:
            self.assertEqual(e["flag"] is not None, e["origin"] == "flag")
            self.assertEqual(e["rule"] is not None, e["origin"] == "rule")

    def test_context_is_the_shared_one_sentence_summary(self):
        for e in EDGES:
            self.assertTrue(e["context"], e["id"])
        row = next(e for e in lc.table_edges() if e.transition.description)
        edge = next(e for e in EDGES if e["id"] == f"{row.source}|{row.verb}")
        self.assertEqual(edge["context"], lc.row_summary(row.transition, row.target))
        self.assertEqual(edge["context"], generate_doc.event_summary(
            row.verb, row.source, row.target, row.verb))
        # A row without a description says what the state it leads to is.
        blank = next(e for e in lc.table_edges() if not e.transition.description)
        text = next(e for e in EDGES if e["id"] == f"{blank.source}|{blank.verb}")["context"]
        self.assertEqual(text, lc.first_sentence(lc.LIFECYCLE[blank.target].description))

    def test_preconditions_merge_the_guards_and_the_requirements_of_the_target(self):
        rows = list(lc.requirement_edges())
        self.assertEqual(len(rows), len(EDGES))
        for e, (_, _, t, target) in zip(EDGES, rows):
            guards = [p for p in e["preconditions"] if isinstance(p, str)]
            invariants = [p for p in e["preconditions"] if isinstance(p, dict)]
            self.assertEqual(guards, [g for g in t.guard if g != lc.ALWAYS], e["id"])
            self.assertEqual([p["invariant"] for p in invariants],
                             list(lc.LIFECYCLE[target].requires), e["id"])
            for p in invariants:
                self.assertIn(p["how"], ("kept", "implied", "established", "masked"), e["id"])
                self.assertIn(p["invariant"], DATA["invariants"])

    def test_anchors_exist_in_the_table_page(self):
        for anchor in {e["anchor"] for e in EDGES} | {n["anchor"] for n in NODES.values()}:
            self.assertIn("{: #" + anchor + " }", TABLE, anchor)
        for key in NODES:
            self.assertIn(f"{{{{ '{gen.GRAPH_PATH}' | relative_url }}}}#"
                          f"{NODES[key]['anchor']})", TABLE)


class TestGraphPage(unittest.TestCase):
    def test_scripts_are_pinned_with_integrity(self):
        tags = re.findall(r"<script [^>]*>", PAGE)
        remote = [t for t in tags if "https://" in t]
        self.assertEqual(len(remote), 3)
        for tag in remote:
            self.assertRegex(tag, r'src="https://cdn\.jsdelivr\.net/npm/[a-z-]+@\d+\.\d+\.\d+/')
            self.assertRegex(tag, r'integrity="sha384-[A-Za-z0-9+/]{64}"')
            self.assertIn('crossorigin="anonymous"', tag)
        local = [t for t in tags if "https://" not in t]
        self.assertEqual(len(local), 1)
        self.assertIn("/assets/js/state-graph.js", local[0])
        self.assertTrue((ROOT / "docs/assets/js/state-graph.js").exists())

    def test_every_kind_has_a_style_variable_and_a_legend_entry(self):
        for kind in lc.KIND_DESCRIPTIONS:
            self.assertIn(f"--sg-kind-{kind}:", SCSS)
            self.assertIn(f'<span class="state state--{kind}">', PAGE)
        self.assertIn("--sg-kind-released:", SCSS)
        for var in KIND_VARS_IN_SCRIPT():
            self.assertIn(f"--sg-kind-{var}:", SCSS)

    def test_page_is_linked_and_loads_the_generated_data(self):
        self.assertIn("permalink: /state-machine/graph/", PAGE)
        self.assertIn("{{ '/state-machine/graph.json' | relative_url }}", PAGE)
        index = (ROOT / "docs/state-machine/index.md").read_text()
        self.assertIn("{{ '/state-machine/graph/' | relative_url }}", index)
        self.assertIn("<noscript>", PAGE)

    def test_no_em_dash(self):
        for text in (PAGE, SCRIPT, SCSS, json.dumps(DATA, ensure_ascii=False)):
            self.assertNotIn(chr(0x2014), text)


def KIND_VARS_IN_SCRIPT():
    m = re.search(r"var KIND_VARS = \[(.*?)\];", SCRIPT, re.S)
    return re.findall(r'"(\w+)"', m.group(1))


if __name__ == "__main__":
    unittest.main()
