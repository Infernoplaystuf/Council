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
