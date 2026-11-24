# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Every caller handles every kind of refusal, explicitly.

``engine.HANDLING`` declares, for each call site and verb, what the site
does with each ``engine.Refusal``; the site acts on it in a ``match`` over
``engine.dispatch`` (or ``engine.applied`` where every kind is a bug). The
tests check the table and read every module: each row taken outside the
engine goes through ``try_apply_*`` and a dispatch of its own site, each
site has its entry and matches every disposition its entry uses, and no
module but the engine names ``_apply_transition`` or ``preconditions``.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

import engine
import lifecycle as lc

ROOT = Path(__file__).resolve().parents[1]
TAKERS = {"engine.try_apply_transition", "engine.try_apply_one_of", "engine.try_apply_command",
          "recovery.fire"}
DISPATCHERS = {"engine.dispatch", "engine.applied"}
DISPOSITIONS = {engine.Skip: "Skip", engine.LateEffect: "LateEffect", engine.Reply: "Reply",
                engine.Bug: "Bug"}


def modules() -> list[Path]:
    return sorted(p for p in ROOT.glob("*.py") if p.name != "engine.py")


def declared(value) -> str:
    """The name of the disposition a ``HANDLING`` value declares."""
    if isinstance(value, engine.Take):
        return "Take"
    return DISPOSITIONS[value]


class Site:
    """The calls of one function that take rows and dispatch them."""

    def __init__(self, module: str, function: str):
        self.module, self.function = module, function
        self.takes: list[ast.Call] = []
        # Rows whose outcome the function returns to its caller, which
        # dispatches it at its own site.
        self.returned: list[ast.Call] = []
        self.dispatches: list[tuple[ast.Call, set[str] | None]] = []
        self.sites: list[tuple] = []
        self.verbs: set[str | None] = set()


def sites() -> dict[tuple[str, str], Site]:
    found: dict[tuple[str, str], Site] = {}
    for path in modules():
        tree = ast.parse(path.read_text())
        module = path.stem
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            site = Site(module, function.name)
            matched: dict[int, set[str]] = {}
            values = [value for node in ast.walk(function) if isinstance(node, ast.Return)
                      for value in ([node.value] if not isinstance(node.value, ast.Tuple)
                                    else node.value.elts)]
            names = {value.id for value in values if isinstance(value, ast.Name)}
            returned = {id(value) for value in values} | {
                id(node.value) for node in ast.walk(function)
                if isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names}
            for node in ast.walk(function):
                if isinstance(node, ast.Match) and isinstance(node.subject, ast.Call) \
                        and ast.unparse(node.subject.func) == "engine.dispatch":
                    names = set()
                    for case in node.cases:
                        pattern = case.pattern
                        if isinstance(pattern, ast.MatchAs) and pattern.pattern is not None:
                            pattern = pattern.pattern
                        if isinstance(pattern, ast.MatchClass):
                            names.add(ast.unparse(pattern.cls).removeprefix("engine."))
                        else:
                            names.add("<default>")
                    matched[id(node.subject)] = names
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                name = ast.unparse(node.func)
                if name in TAKERS and id(node) in returned:
                    site.returned.append(node)
                elif name in TAKERS:
                    site.takes.append(node)
                    if name == "engine.try_apply_transition" and len(node.args) > 1 \
                            and isinstance(node.args[1], ast.Constant):
                        site.verbs.add(node.args[1].value)
                    else:
                        site.verbs.add(None)
                elif name in DISPATCHERS:
                    site.dispatches.append((node, matched.get(id(node))
                                            if name == "engine.dispatch" else {"Applied", "Bug"}))
                    if len(node.args) > 1:
                        site.sites.append(ast.literal_eval(node.args[1]))
            if site.takes or site.dispatches or site.returned:
                found[(module, function.name)] = site
    return found


class TestTheTable(unittest.TestCase):
    def test_every_entry_names_every_kind(self):
        for key, entry in engine.HANDLING.items():
            with self.subTest(site=key):
                self.assertEqual(set(entry), set(engine.Refusal))
                for kind, value in entry.items():
                    self.assertIn(declared(value), {"Skip", "Take", "LateEffect", "Reply", "Bug"})

    def test_every_take_names_a_row_of_the_same_state(self):
        for (module, function, group), entry in engine.HANDLING.items():
            verbs = [v for v in group.split("|") if v]
            for kind, value in entry.items():
                if not isinstance(value, engine.Take):
                    continue
                with self.subTest(site=(module, function, group), kind=kind):
                    states = {e.source for e in lc.table_edges() if e.verb in verbs}
                    taken = {e.source for e in lc.table_edges() if e.verb == value.verb}
                    self.assertTrue(states & taken, (states, value.verb))
                    if kind == engine.Refusal.BUDGET_EXHAUSTED:
                        # The exhaustion row of a budget the refused row
                        # needs to remain.
                        needed = {name for e in lc.table_edges() if e.verb in verbs
                                  for guard in e.transition.guard
                                  if engine.PREDICATES[guard].refusal
                                  == engine.Refusal.BUDGET_EXHAUSTED
                                  for name in lc.BUDGETS if name in guard}
                        exhaustions = {v for name in needed
                                       for v in lc.BUDGETS[name].exhaustion_verbs}
                        self.assertIn(value.verb, exhaustions, needed)

    def test_every_entry_is_a_function_that_takes_rows(self):
        found = sites()
        for module, function, _group in engine.HANDLING:
            if module == "engine":
                continue
            with self.subTest(site=(module, function)):
                self.assertIn((module, function), found)

    def test_a_refusal_without_an_entry_is_an_error(self):
        refused = engine.Refused(engine.Refusal.GUARD_UNMET, "request_patch", None, "x")
        with self.assertRaises(lc.TransitionError):
            engine.dispatch(refused, ("nowhere", "nothing"))


class TestTheCallSites(unittest.TestCase):
    def test_every_row_taken_is_dispatched_at_its_own_site(self):
        for (module, function), site in sites().items():
            with self.subTest(site=(module, function)):
                # Every outcome of a row reaches a dispatch of this site.
                self.assertTrue(site.dispatches or not site.takes,
                                "a row is taken without a dispatch")
                for named in site.sites:
                    self.assertEqual(named, (module, function))

    def test_every_site_matches_the_dispositions_it_declares(self):
        for (module, function), site in sites().items():
            entries = {group: entry for (m, f, group), entry in engine.HANDLING.items()
                       if (m, f) == (module, function)}
            if not site.dispatches:
                continue
            with self.subTest(site=(module, function)):
                self.assertTrue(entries, "no handling is declared")
                cases = set().union(*(names or set() for _call, names in site.dispatches))
                self.assertNotIn("<default>", cases, "a match with a default arm")
                self.assertIn("Applied", cases)
                used = {declared(value) for entry in entries.values() for value in entry.values()}
                if all(name == "engine.applied" for name in
                       (ast.unparse(call.func) for call, _names in site.dispatches)):
                    self.assertEqual(used, {"Bug"}, "engine.applied at a site that does not "
                                                    "declare every kind a bug")
                self.assertLessEqual(used, cases)
                for verb in site.verbs - {None}:
                    self.assertTrue(any(verb in group.split("|") for group in entries), verb)

    def test_only_the_engine_names_its_private_api(self):
        for path in modules():
            tree = ast.parse(path.read_text())
            names = {ast.unparse(n) for n in ast.walk(tree)
                     if isinstance(n, (ast.Attribute, ast.Name))}
            with self.subTest(module=path.stem):
                for private in ("engine._apply_transition", "engine.preconditions",
                                "engine.apply_transition", "engine.refusal",
                                "engine.resolve_command", "engine.apply_command",
                                "engine._evaluate", "engine._guards"):
                    self.assertNotIn(private, names)
        self.assertEqual(lc.private_engine_uses(ROOT), [])

    def test_the_read_only_query_is_asked_only_where_it_is_allowed(self):
        for path in modules():
            tree = ast.parse(path.read_text())
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(function):
                    if isinstance(node, ast.Call) \
                            and ast.unparse(node.func) == "engine.row_admitted":
                        with self.subTest(site=(path.stem, function.name)):
                            self.assertIn((path.stem, function.name), engine.ROW_QUERY_SITES)


if __name__ == "__main__":
    unittest.main()
