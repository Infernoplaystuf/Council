"""
tests.source_checks — reading code without reading the prose about it.

THREE separate tests in this port have failed on the docstring that EXPLAINS
the defect they were checking for. A test proving an id is not recovered from a
label trips on the sentence saying the Tk version does exactly that; the same
happened with `SpecialistRegistry()`, and a probe counted a finished handler as
an unfinished stub because it discussed the Council tab.

A source search cannot tell a call from a mention. Prose about a bug is the
most valuable thing in the file and is the last thing a check should read, so
it is stripped here once instead of being worked around again.
"""
from __future__ import annotations

def code_of(source_or_node, name=None):
    """A function's source with its DOCSTRING REMOVED.

    WHY THIS EXISTS: three separate tests in this port have failed on the
    docstring that EXPLAINS the defect they were checking for. A test that
    searches for `split(` to prove an id is not recovered from a label trips on
    the sentence saying the Tk version does exactly that; the same happened with
    `SpecialistRegistry()` and with a probe that counted a finished handler as a
    stub for discussing the Council tab.

    A source search cannot tell a call from a mention. Prose about a bug is the
    most valuable thing in the file and is the last thing a check should read,
    so it is stripped here once rather than worked around three more times.

    Pass source text plus a function name, or an ast node.
    """
    import ast

    node = source_or_node
    if isinstance(source_or_node, str):
        tree = ast.parse(source_or_node)
        node = next(
            (n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)) and n.name == name),
            None)
        if node is None:
            raise AssertionError(f"no definition of {name!r} to read")
        source = source_or_node
    else:
        import inspect
        source = inspect.getsource(node)
        node = ast.parse(source.strip()).body[0]

    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]                       # drop the docstring
    if not body:
        return ""
    return "\n".join(ast.unparse(statement) for statement in body)


def widget_touches_in_worker(source: str, func_name: str,
                             widgets) -> list:
    """Named widgets a worker touches OUTSIDE the marshalling seam.

    WHY AN AST AND NOT A STRING SPLIT
    Five test files check this by slicing the function text between "def work"
    and "def show" and searching for `self.<widget>.`. That only works when the
    worker happens to define an inner `show`; a worker that marshals with
    `self._to_ui(lambda: self.pane.setText(x))` has its widget call inside the
    seam and the slice reports it as a violation. It did, for the Grapher's
    stats refresh, where the code was right and the check was wrong.

    TWO SHAPES OF SEAM, AND THE FIRST VERSION OF THIS ONLY KNEW ONE
    `_to_ui(lambda: ...)` passes the body inline; `_to_ui(show)` passes a
    nested function BY NAME, and walking the argument then reaches a bare Name
    and none of the code it stands for. Both are exempt here.

    `widgets` is the list of attribute names that are actually widgets. Asked
    for rather than guessed: `self.actions.session` is `self.<x>.<y>` too, and
    a check that flagged it would be one people learn to ignore.
    """
    import ast

    tree = ast.parse(source)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))                 and node.name == func_name:
            target = node
            break
    if target is None:
        raise AssertionError(f"no function named {func_name!r}")

    nested = {n.name: n for n in ast.walk(target)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n is not target}
    if not nested:
        return []

    exempt_nodes = set()
    exempt_names = set()
    for node in ast.walk(target):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_to_ui"):
            continue
        for argument in list(node.args) + [k.value for k in node.keywords]:
            if isinstance(argument, ast.Name):
                exempt_names.add(argument.id)          # _to_ui(show)
            elif isinstance(argument, ast.Attribute):
                exempt_names.add(argument.attr)        # _to_ui(self.done)
            for inner in ast.walk(argument):
                exempt_nodes.add(id(inner))            # _to_ui(lambda: ...)

    # A function passed to _to_ui by NAME is usually nested INSIDE the worker,
    # so walking the worker descends into it. Its whole subtree is exempt, not
    # just its definition — which is the difference between this reporting the
    # Grapher's interactive path as a violation and not.
    for name, function in nested.items():
        if name in exempt_names:
            for inner in ast.walk(function):
                exempt_nodes.add(id(inner))

    wanted = set(widgets)
    found = []
    for name, worker in nested.items():
        if name in exempt_names:
            continue
        for node in ast.walk(worker):
            if id(node) in exempt_nodes:
                continue
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Attribute)
                    and isinstance(node.value.value, ast.Name)
                    and node.value.value.id == "self"
                    and node.value.attr in wanted):
                found.append(f"self.{node.value.attr}.{node.attr}")
    return sorted(set(found))
