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


STATIC_TAKERS = frozenset({"engine.try_apply_transition", "engine.try_apply_one_of",
                           "engine.try_apply_command"})


def called(call: ast.Call, module: str, bindings) -> str | None:
    """The qualified name a call resolves to: ``engine.<name>`` for the
    engine under any alias (`import engine as e`, `from engine import x`,
    ``getattr(engine, "x")``), else ``<module>.<name>`` for another module's
    attribute or a name of this module."""
    modules, names = bindings
    func = call.func
    if isinstance(func, ast.Call) and isinstance(func.func, ast.Name) \
            and func.func.id == "getattr" and len(func.args) > 1 \
            and isinstance(func.args[0], ast.Name) and func.args[0].id in modules \
            and isinstance(func.args[1], ast.Constant):
        return f"engine.{func.args[1].value}"
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"engine.{func.attr}" if func.value.id in modules else \
            f"{func.value.id}.{func.attr}"
    if isinstance(func, ast.Name):
        return f"engine.{names[func.id]}" if func.id in names else f"{module}.{func.id}"
    return None


def taker_references(tree, bindings) -> list[tuple[ast.AST, str]]:
    """Every reference to a function that takes a row (``STATIC_TAKERS``) in
    a module, the engine under any alias: the node that names it (the
    function of a call, or any other use of the name) and the name."""
    modules, names = bindings
    found = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id in modules:
            name = f"engine.{node.attr}"
        elif isinstance(node, ast.Name) and node.id in names \
                and isinstance(node.ctx, ast.Load):
            name = f"engine.{names[node.id]}"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "getattr" and len(node.args) > 1 \
                and isinstance(node.args[0], ast.Name) and node.args[0].id in modules \
                and isinstance(node.args[1], ast.Constant):
            name = f"engine.{node.args[1].value}"
        if name in STATIC_TAKERS:
            found.append((node, name))
    return found


def own_nodes(function):
    """The nodes of a function, but those of the functions defined in it
    (each is analysed on its own)."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def analyse(sources: dict[str, str]) -> tuple[dict[tuple[str, str], Site], list[str]]:
    """Every function of ``sources`` (module name to source) that takes rows
    and the problems found: each take must be consumed by exactly one
    dispatch, in the function (an argument of it, or a name it dispatches
    once per assignment) or in the caller of a function that returns it, and
    every function that returns a take has a caller, which takes it in turn.
    The engine under any alias is resolved (``called``)."""
    trees = {module: ast.parse(text) for module, text in sources.items()}
    bindings = {module: lc.engine_bindings(tree) for module, tree in trees.items()}
    functions = [(module, node) for module, tree in trees.items() for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    takers = set(STATIC_TAKERS) | {"recovery.fire"}
    found: dict[tuple[str, str], Site] = {}
    problems: list[str] = []
    # A taker is only ever called, directly, in a function: not bound to a
    # name, passed to `functools.partial` or any other function, called in
    # a lambda, or called at module level.
    called_here = {id(call.func) for module, function in functions
                   for call in own_nodes(function) if isinstance(call, ast.Call)
                   and called(call, module, bindings[module]) in STATIC_TAKERS}
    reference_problems = [
        f"{module}:{node.lineno}: {name} is not called directly in a function"
        for module, tree in trees.items()
        for node, name in taker_references(tree, bindings[module])
        if id(node) not in called_here]
    while True:
        found, problems = {}, []
        grown = False
        for module, function in functions:
            where = f"{module}.{function.name}"
            site = Site(module, function.name)
            values = [value for node in own_nodes(function) if isinstance(node, ast.Return)
                      and node.value is not None
                      for value in (node.value.elts if isinstance(node.value, ast.Tuple)
                                    else [node.value])]
            returned_names = {value.id for value in values if isinstance(value, ast.Name)}
            returned = {id(value) for value in values}
            calls = [node for node in own_nodes(function) if isinstance(node, ast.Call)]
            take_calls = [c for c in calls if called(c, module, bindings[module]) in takers]
            dispatch_calls = [c for c in calls
                              if called(c, module, bindings[module]) in
                              ("engine.dispatch", "engine.applied")]
            assigned: dict[str, list[ast.Call]] = {}
            for node in own_nodes(function):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                        and isinstance(node.value, ast.Call) and node.value in take_calls:
                    target = node.targets[0]
                    for name in (target.elts if isinstance(target, ast.Tuple) else [target]):
                        if isinstance(name, ast.Name):
                            assigned.setdefault(name.id, []).append(node.value)
            consumed: dict[int, int] = {}
            for dispatch in dispatch_calls:
                arg = dispatch.args[0] if dispatch.args else None
                if isinstance(arg, ast.Call) and arg in take_calls:
                    consumed[id(arg)] = consumed.get(id(arg), 0) + 1
                elif isinstance(arg, ast.Name) and arg.id in assigned:
                    for take in assigned[arg.id]:
                        consumed[id(take)] = consumed.get(id(take), 0) + 1
                else:
                    problems.append(f"{where}: a dispatch of something that is not the outcome "
                                    "of a row taken here")
            for name, takes in assigned.items():
                uses = sum(1 for d in dispatch_calls
                           if d.args and isinstance(d.args[0], ast.Name) and d.args[0].id == name)
                if uses > len(takes):
                    problems.append(f"{where}: the outcome {name} is dispatched {uses} times")
            for take in take_calls:
                is_returned = id(take) in returned or any(
                    take in takes and name in returned_names for name, takes in assigned.items())
                if is_returned:
                    site.returned.append(take)
                    if where not in takers:
                        takers.add(where)
                        grown = True
                    if consumed.get(id(take)):
                        problems.append(f"{where}: a row is returned and dispatched")
                elif not consumed.get(id(take)):
                    problems.append(f"{where}:{take.lineno}: the outcome of a row is not "
                                    "dispatched")
                else:
                    site.takes.append(take)
                    name = called(take, module, bindings[module])
                    if name == "engine.try_apply_transition" and len(take.args) > 1 \
                            and isinstance(take.args[1], ast.Constant):
                        site.verbs.add(take.args[1].value)
                    else:
                        site.verbs.add(None)
            for dispatch in dispatch_calls:
                kind = called(dispatch, module, bindings[module])
                names = None
                if kind == "engine.dispatch":
                    parent = next((n for n in own_nodes(function) if isinstance(n, ast.Match)
                                   and n.subject is dispatch), None)
                    names = set()
                    for case in (parent.cases if parent else []):
                        pattern = case.pattern
                        if isinstance(pattern, ast.MatchAs) and pattern.pattern is not None:
                            pattern = pattern.pattern
                        if isinstance(pattern, ast.MatchClass):
                            names.add(ast.unparse(pattern.cls).removeprefix("engine."))
                        else:
                            names.add("<default>")
                    if parent is None:
                        names = None
                site.dispatches.append((dispatch, names if kind == "engine.dispatch"
                                        else {"Applied", "Bug"}))
                if len(dispatch.args) > 1:
                    site.sites.append(ast.literal_eval(dispatch.args[1]))
            if site.takes or site.dispatches or site.returned:
                found[(module, function.name)] = site
        if not grown:
            break
    for key in sorted(name for name in takers if name not in STATIC_TAKERS):
        module, _, function = key.partition(".")
        callers = [1 for m, node in functions for c in ast.walk(node)
                   if isinstance(c, ast.Call) and called(c, m, bindings[m]) == key]
        if not callers and (module, function) in found:
            problems.append(f"{key}: returns the outcome of a row to no caller")
    return found, problems + reference_problems


def sites() -> dict[tuple[str, str], Site]:
    return analyse({path.stem: path.read_text() for path in modules()})[0]


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
    def test_every_outcome_of_a_row_is_dispatched_once_or_returned_to_a_dispatching_caller(self):
        _found, problems = analyse({path.stem: path.read_text() for path in modules()})
        self.assertEqual(problems, [])

    def test_the_lint_resolves_aliases_and_finds_what_is_not_dispatched(self):
        bad = {
            "a": "import engine as e\n"
                 "def f(r):\n    e.try_apply_transition(r, 'x')\n",
            "b": "from engine import try_apply_transition as t\n"
                 "def f(r):\n    out = t(r, 'x')\n    return 1\n",
            "c": "import engine\n"
                 "def f(r):\n    return getattr(engine, 'try_apply_transition')(r, 'x')\n",
            "d": "import engine\n"
                 "def f(r):\n    out = engine.try_apply_transition(r, 'x')\n"
                 "    engine.dispatch(out, ('d', 'f'))\n    engine.dispatch(out, ('d', 'f'))\n",
            # A taker that is bound to a name, handed to another function,
            # or called in a lambda or at module level.
            "e": "import engine\ndef f(r):\n    t = engine.try_apply_transition\n    t(r, 'x')\n",
            "f": "import engine, functools\n"
                 "def f(r):\n    functools.partial(engine.try_apply_transition, r)('x')\n",
            "g": "import engine\nTAKE = lambda r: engine.try_apply_transition(r, 'x')\n",
            "h": "import engine\ndef f(r):\n    g = lambda: engine.try_apply_transition(r, 'x')\n"
                 "    return g\n",
            "i": "import engine\nengine.try_apply_transition({}, 'x')\n",
        }
        for module, source in bad.items():
            with self.subTest(module=module):
                _found, problems = analyse({module: source})
                self.assertTrue(problems, source)
        good = {"g": "import engine as e\n"
                     "def f(r):\n    out = e.try_apply_transition(r, 'x')\n"
                     "    match e.dispatch(out, ('g', 'f')):\n"
                     "        case e.Applied():\n            pass\n"}
        self.assertEqual(analyse(good)[1], [])

    def test_the_private_api_is_found_under_any_alias(self):
        for source in ("import engine as e\ne.preconditions\n",
                       "from engine import preconditions\n",
                       "from engine import *\n",
                       "import engine\ngetattr(engine, 'apply_transition')\n",
                       "import engine\ngetattr(engine, name)\n",
                       "import engine\nx = engine\nx._evaluate\n",
                       "import engine\nengine.__dict__['_apply_transition']\n",
                       "import engine\nvars(engine)['apply_transition']\n",
                       "import importlib\nimportlib.import_module('engine').preconditions\n",
                       "import sys\nsys.modules['engine'].preconditions\n",
                       "__import__('engine').preconditions\n"):
            with self.subTest(source=source):
                self.assertTrue(lc.private_engine_names_in(ast.parse(source)), source)
        self.assertEqual(lc.private_engine_names_in(ast.parse(
            "import engine as e\ne.try_apply_transition\n")), [])

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
